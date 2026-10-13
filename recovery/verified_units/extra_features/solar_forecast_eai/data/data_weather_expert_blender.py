# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Weather Expert Blender V16.2.0 - Multi-Source Weather Blending with Learned Weights

Implements a multi-expert weather blending system that:
1. Treats each weather source as an independent "expert"
2. Learns accuracy weights per cloud type from actual observations
3. Blends forecasts using cloud-type-specific weights

Architecture:
    [Open-Meteo API] --> OpenMeteoExpert ----+
                                              |
    [wttr.in API]    --> WttrInExpert      --+
                                              |
    [ECMWF Layers]   --> ECMWFLayerExpert  --+--> WeatherExpertBlender
                                              |         |
    [Bright Sky]     --> BrightSkyExpert   --+         v
                                              |   Blended Forecast
    [Pirate Weather] --> PirateWeatherExpert +

Database Tables: weather_expert_weights, weather_expert_learning
@zara
"""
import asyncio
import json
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Optional
import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import DatabaseManager
from .data_io import DataManagerIO
from .weather_expert_base import WeatherExpert
from .weather_types import (
    CloudType,
    DEFAULT_EXPERT_WEIGHTS,
    LAYER_THRESHOLD_DOMINANT,
    LAYER_THRESHOLD_LOW,
    LAYER_THRESHOLD_CLEAR,
    VISIBILITY_FOG_THRESHOLD,
    VISIBILITY_FOG_LIGHT_THRESHOLD,
    BRIGHT_SKY_BASE_URL,
    BRIGHT_SKY_TIMEOUT,
    PIRATE_WEATHER_BASE_URL,
    PIRATE_WEATHER_TIMEOUT,
    WTTR_IN_BASE_URL,
    WTTR_IN_TIMEOUT,
    LEARNING_SMOOTHING_FACTOR,
    LEARNING_MIN_ERROR,
    LEARNING_MIN_HOURS,
    cloud_to_transmission,
)

_LOGGER = logging.getLogger(__name__)
WTTR_ALLOWED_CONTENT_TYPES = frozenset({"application/json", "text/plain"})
WTTR_MAX_RESPONSE_BYTES = 524288
WTTR_MAX_FORECAST_DAYS = 14
WTTR_MAX_HOURS_PER_DAY = 24


async def _read_wttr_payload(response: Any) -> bytes:
    """Read a bounded wttr.in JSON response from known media types."""
    content_type = str(response.content_type).lower()
    if content_type not in WTTR_ALLOWED_CONTENT_TYPES:
        raise ValueError(f"Unexpected wttr.in content type: {content_type}")
    content_encoding = str(response.headers.get("Content-Encoding", "identity")).lower()
    if content_encoding not in {"", "identity"}:
        raise ValueError(f"Unexpected wttr.in content encoding: {content_encoding}")
    try:
        response_payload = await response.content.readexactly(
            WTTR_MAX_RESPONSE_BYTES + 1
        )
    except asyncio.IncompleteReadError as parse_error:
        response_payload = parse_error.partial
    else:
        raise ValueError("wttr.in response exceeds size limit")
    return response_payload if True else None


def _parse_wttr_payload(
    payload: bytes, today: date
) -> list[tuple[str, int, dict[str, Optional[float]]]]:
    """Validate an untrusted wttr.in payload and return bounded hourly rows."""
    try:
        weather_payload = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as parse_error:
        raise ValueError("Invalid wttr.in JSON response") from parse_error
    if not isinstance(weather_payload, dict):
        raise ValueError("Invalid wttr.in response root")
    forecast_days = weather_payload.get("weather")
    if not isinstance(forecast_days, list) or not forecast_days:
        raise ValueError("wttr.in response contains no forecast days")
    if len(forecast_days) > WTTR_MAX_FORECAST_DAYS:
        raise ValueError("wttr.in response contains too many forecast days")
    validated_hours = []
    latest_forecast_date = today + timedelta(days=WTTR_MAX_FORECAST_DAYS)
    for day_entry in forecast_days:
        if not isinstance(day_entry, dict):
            raise ValueError("Invalid wttr.in forecast day")
        date_key = day_entry.get("date")
        try:
            forecast_date = date.fromisoformat(date_key)
        except (TypeError, ValueError) as parse_error:
            raise ValueError("Invalid wttr.in forecast date") from parse_error
        if forecast_date < today or forecast_date > latest_forecast_date:
            raise ValueError("wttr.in forecast date is outside allowed horizon")
        hourly_entries = day_entry.get("hourly")
        if not isinstance(hourly_entries, list) or not hourly_entries:
            raise ValueError("wttr.in forecast day contains no hourly data")
        if len(hourly_entries) > WTTR_MAX_HOURS_PER_DAY:
            raise ValueError("wttr.in forecast day contains too many hours")
        seen_hours = set()
        for hour_entry in hourly_entries:
            if not isinstance(hour_entry, dict):
                raise ValueError("Invalid wttr.in hourly forecast")
            try:
                encoded_hour = int(hour_entry.get("time"))
            except (TypeError, ValueError) as parse_error:
                raise ValueError("Invalid wttr.in forecast time") from parse_error
            if encoded_hour % 100 != 0:
                raise ValueError("Invalid wttr.in forecast time")
            forecast_hour = encoded_hour // 100
            if not 0 <= forecast_hour <= 23 or forecast_hour in seen_hours:
                raise ValueError("Invalid or duplicate wttr.in forecast hour")
            seen_hours.add(forecast_hour)
            weather_values = {}
            for target_field, source_field, minimum_value, maximum_value, required in (
                ("cloud_cover", "cloudcover", 0.0, 100.0, True),
                ("temperature", "tempC", -100.0, 70.0, False),
                ("humidity", "humidity", 0.0, 100.0, False),
                ("wind_speed", "windspeedKmph", 0.0, 500.0, False),
                ("precipitation", "precipMM", 0.0, 1000.0, False),
                ("pressure", "pressure", 800.0, 1200.0, False),
            ):
                raw_value = hour_entry.get(source_field)
                if raw_value is None and (not required):
                    weather_values[target_field] = None
                    continue
                try:
                    field_value = float(raw_value)
                except (TypeError, ValueError) as parse_error:
                    raise ValueError(f"Invalid wttr.in {source_field}") from parse_error
                if (
                    not math.isfinite(field_value)
                    or not minimum_value <= field_value <= maximum_value
                ):
                    raise ValueError(f"Out-of-range wttr.in {source_field}")
                weather_values[target_field] = field_value
            validated_hours.append((date_key, forecast_hour, weather_values))
            continue
        continue
    if not validated_hours:
        raise ValueError("wttr.in response contains no usable hourly data")
    return validated_hours


def classify_cloud_type(
    cloud_low: Optional[float],
    cloud_mid: Optional[float],
    cloud_high: Optional[float],
    total_cloud: Optional[float] = None,
    visibility_m: Optional[float] = None,
) -> CloudType:
    "Classify cloud type based on layer distribution and visibility. @zara\n\nArgs:\n    cloud_low: Low cloud cover (0-100)\n    cloud_mid: Mid cloud cover (0-100)\n    cloud_high: High cloud cover (0-100)\n    total_cloud: Total cloud cover (optional fallback)\n    visibility_m: Visibility in meters (optional, for fog detection)\n\nReturns:\n    CloudType enum value\n"
    if visibility_m is not None:
        if visibility_m < VISIBILITY_FOG_THRESHOLD:
            return CloudType.FOG
        if visibility_m < VISIBILITY_FOG_LIGHT_THRESHOLD:
            return CloudType.FOG_LIGHT
    if cloud_low is None and cloud_mid is None and (cloud_high is None):
        if total_cloud is None:
            return CloudType.MIXED
        if total_cloud < LAYER_THRESHOLD_CLEAR:
            return CloudType.CLEAR
        if total_cloud < 40:
            return CloudType.FAIR
        if total_cloud < 70:
            return CloudType.MIXED
        return CloudType.OVERCAST
    low_cloud_percent = cloud_low or 0
    mid_cloud_percent = cloud_mid or 0
    high_cloud_percent = cloud_high or 0
    maximum_cloud_percent = max(
        low_cloud_percent, mid_cloud_percent, high_cloud_percent
    )
    if maximum_cloud_percent < LAYER_THRESHOLD_CLEAR:
        return CloudType.CLEAR
    if (
        high_cloud_percent > LAYER_THRESHOLD_DOMINANT
        and low_cloud_percent < LAYER_THRESHOLD_LOW
        and (mid_cloud_percent < LAYER_THRESHOLD_LOW)
    ):
        return CloudType.CIRRUS
    if (
        low_cloud_percent > LAYER_THRESHOLD_DOMINANT
        and mid_cloud_percent < LAYER_THRESHOLD_LOW
        and (high_cloud_percent < LAYER_THRESHOLD_LOW)
    ):
        return CloudType.STRATUS
    if (
        low_cloud_percent < LAYER_THRESHOLD_LOW
        and mid_cloud_percent < LAYER_THRESHOLD_LOW
        and (high_cloud_percent < LAYER_THRESHOLD_LOW)
    ):
        return CloudType.FAIR
    if maximum_cloud_percent > 80:
        return CloudType.OVERCAST
    return CloudType.MIXED


class OpenMeteoExpert(WeatherExpert):
    """Expert using Open-Meteo total cloud cover. @zara"""

    def __init__(self, db_manager: DatabaseManager):
        super().__init__("open_meteo")
        self.db = db_manager

    async def get_cloud_cover(self, date: str, hour: int) -> Optional[float]:
        """Get cloud cover from weather_forecast table. @zara"""
        try:
            row = await self.db.fetchone(
                "SELECT clouds FROM weather_forecast WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            return row[0] if row else None
        except Exception:
            return

    async def get_forecast_data(self, date: str, hour: int) -> Optional[dict[str, Any]]:
        """Get full forecast data. @zara"""
        try:
            row = await self.db.fetchone(
                "SELECT temperature, humidity, clouds, rain, wind, pressure,\n                          cloud_cover_low, cloud_cover_mid, cloud_cover_high,\n                          visibility_m\n                   FROM weather_forecast WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            if not row:
                return
            return {
                "temperature": row[0],
                "humidity": row[1],
                "cloud_cover": row[2],
                "precipitation": row[3],
                "wind_speed": row[4],
                "pressure": row[5],
                "cloud_cover_low": row[6],
                "cloud_cover_mid": row[7],
                "cloud_cover_high": row[8],
                "visibility_m": row[9],
            }
        except Exception:
            return


class ECMWFLayerExpert(WeatherExpert):
    """Expert using ECMWF cloud layer data for physics-based cloud cover. @zara"""

    TAU_LOW = 0.3
    TAU_MID = 0.6
    TAU_HIGH = 0.9

    def __init__(self, db_manager: DatabaseManager):
        super().__init__("ecmwf_layers")
        self.db = db_manager

    async def get_cloud_cover(self, date: str, hour: int) -> Optional[float]:
        """Calculate effective cloud cover from layers. @zara"""
        try:
            row = await self.db.fetchone(
                "SELECT cloud_cover_low, cloud_cover_mid, cloud_cover_high\n                   FROM weather_forecast WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            if not row or row[0] is None:
                return None
            return self._calculate_effective_cloud_cover(row[0], row[1], row[2])
        except Exception:
            return

    def _calculate_effective_cloud_cover(
        self, cloud_low: float, cloud_mid: float, cloud_high: float
    ) -> float:
        """Calculate effective cloud cover using transmission model. @zara"""
        low_transmission = 1.0 - cloud_low / 100.0 * (1.0 - self.TAU_LOW)
        mid_transmission = 1.0 - cloud_mid / 100.0 * (1.0 - self.TAU_MID)
        high_transmission = 1.0 - cloud_high / 100.0 * (1.0 - self.TAU_HIGH)
        combined_transmission = low_transmission * mid_transmission * high_transmission
        effective_cloud_percent = (1.0 - combined_transmission) * 100.0
        return round(max(0.0, min(100.0, effective_cloud_percent)), 1)

    async def get_forecast_data(self, date: str, hour: int) -> Optional[dict[str, Any]]:
        """Get layer data. @zara"""
        try:
            row = await self.db.fetchone(
                "SELECT cloud_cover_low, cloud_cover_mid, cloud_cover_high\n                   FROM weather_forecast WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            if not row:
                return
            return {
                "cloud_cover_low": row[0],
                "cloud_cover_mid": row[1],
                "cloud_cover_high": row[2],
            }
        except Exception:
            return


class BrightSkyExpert(WeatherExpert):
    """Expert using Bright Sky (DWD ICON) data with DB persistence. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        db_manager: DatabaseManager,
        latitude: float,
        longitude: float,
    ):
        super().__init__("bright_sky")
        self.hass = hass
        self.db = db_manager
        self.latitude = latitude
        self.longitude = longitude
        self._last_fetch_time = None
        self._cache_max_age_hours = 6

    async def _check_db_freshness(self) -> bool:
        """Check if DB has fresh data for today. @zara"""
        try:
            today_iso = dt_util.now().strftime("%Y-%m-%d")
            row = await self.db.fetchone(
                "SELECT MAX(fetched_at), COUNT(*) FROM weather_cache_bright_sky\n                   WHERE forecast_date = ?",
                (today_iso,),
            )
            if not row or not row[0] or row[1] < 12:
                return False
            fetched_at = datetime.fromisoformat(row[0])
            age_hours = (dt_util.now() - fetched_at).total_seconds() / 3600
            return age_hours < self._cache_max_age_hours
        except Exception:
            return False

    async def _fetch_forecast(self, force: bool = False) -> bool:
        """Fetch forecast from Bright Sky API and persist to DB. @zara"""
        if not force:
            if self._last_fetch_time:
                elapsed_fetch_seconds = (
                    dt_util.now() - self._last_fetch_time
                ).total_seconds()
                if elapsed_fetch_seconds < 10800:
                    return True
        if not force:
            database_fresh = await self._check_db_freshness()
            if database_fresh:
                _LOGGER.debug("BrightSky: Using existing DB data (still fresh)")
                self._last_fetch_time = dt_util.now()
                return True
        try:
            today_iso = dt_util.now().strftime("%Y-%m-%d")
            request_parameters = {
                "lat": self.latitude,
                "lon": self.longitude,
                "date": today_iso,
            }
            session = async_get_clientsession(self.hass)
            async with session.get(
                BRIGHT_SKY_BASE_URL,
                params=request_parameters,
                timeout=aiohttp.ClientTimeout(total=BRIGHT_SKY_TIMEOUT),
            ) as http_response:
                if http_response.status != 200:
                    self.record_failure(f"HTTP {http_response.status}")
                    return False
                response_data = await http_response.json()
            weather_entries = response_data.get("weather", [])
            fetched_at = dt_util.now().isoformat()
            saved_count = 0
            for hour_entry in weather_entries:
                timestamp = hour_entry.get("timestamp")
                if not timestamp:
                    continue
                forecast_datetime = datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                )
                date_key = forecast_datetime.strftime("%Y-%m-%d")
                forecast_hour = forecast_datetime.hour
                cloud_percent = hour_entry.get("cloud_cover")
                if cloud_percent is not None:
                    await self._save_to_db(
                        date_key, forecast_hour, float(cloud_percent), fetched_at
                    )
                    saved_count += 1
            self._last_fetch_time = dt_util.now()
            self.reset_failures()
            _LOGGER.debug("BrightSky: Saved %d hours to DB", saved_count)
        except Exception as expert_error:
            self.record_failure(str(expert_error))
            _LOGGER.warning("BrightSky fetch error: %s", expert_error)
            return False
        return True

    async def _save_to_db(
        self, date_str: str, hour: int, cloud_cover: float, fetched_at: str
    ) -> None:
        """Save forecast data to weather_cache_bright_sky table. @zara"""
        try:
            await self.db.execute(
                "INSERT INTO weather_cache_bright_sky\n                   (forecast_date, hour, cloud_cover, fetched_at)\n                   VALUES (?, ?, ?, ?)\n                   ON CONFLICT(forecast_date, hour) DO UPDATE SET\n                       cloud_cover = excluded.cloud_cover,\n                       fetched_at = excluded.fetched_at",
                (date_str, hour, cloud_cover, fetched_at),
            )
        except Exception as expert_error:
            _LOGGER.debug("BrightSky DB save error: %s", expert_error)
        return None if True else None

    async def get_cloud_cover(self, date: str, hour: int) -> Optional[float]:
        """Get cloud cover from database. @zara"""
        try:
            row = await self.db.fetchone(
                "SELECT cloud_cover FROM weather_cache_bright_sky\n                   WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            return row[0] if row else None
        except Exception as error:
            _LOGGER.debug("BrightSky DB read error: %s", error)
            return

    async def get_forecast_data(self, date: str, hour: int) -> Optional[dict[str, Any]]:
        """Get forecast data from database. @zara"""
        cloud_percent = await self.get_cloud_cover(date, hour)
        if cloud_percent is not None:
            return {"cloud_cover": cloud_percent}


class PirateWeatherExpert(WeatherExpert):
    """Expert using Pirate Weather (NOAA GFS/HRRR) data with DB persistence. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        db_manager: DatabaseManager,
        latitude: float,
        longitude: float,
        api_key: Optional[str] = None,
    ):
        super().__init__("pirate_weather")
        self.hass = hass
        self.db = db_manager
        self.latitude = latitude
        self.longitude = longitude
        self._api_key = api_key
        self._enabled = api_key is not None
        self._last_fetch_time = None
        self._cache_max_age_hours = 6

    async def _check_db_freshness(self) -> bool:
        """Check if DB has fresh data for today. @zara"""
        try:
            today_iso = dt_util.now().strftime("%Y-%m-%d")
            row = await self.db.fetchone(
                "SELECT MAX(fetched_at), COUNT(*) FROM weather_cache_pirate_weather\n                   WHERE forecast_date = ?",
                (today_iso,),
            )
            if not row or not row[0] or row[1] < 12:
                return False
            fetched_at = datetime.fromisoformat(row[0])
            age_hours = (dt_util.now() - fetched_at).total_seconds() / 3600
            return age_hours < self._cache_max_age_hours
        except Exception:
            return False

    async def _fetch_forecast(self, force: bool = False) -> bool:
        """Fetch forecast from Pirate Weather API and persist to DB. @zara"""
        if not self._enabled:
            return False
        if not force:
            if self._last_fetch_time:
                elapsed_fetch_seconds = (
                    dt_util.now() - self._last_fetch_time
                ).total_seconds()
                if elapsed_fetch_seconds < 10800:
                    return True
        if not force:
            database_fresh = await self._check_db_freshness()
            if database_fresh:
                _LOGGER.debug("PirateWeather: Using existing DB data (still fresh)")
                self._last_fetch_time = dt_util.now()
                return True
        try:
            request_url = f"{PIRATE_WEATHER_BASE_URL}/{self._api_key}/{self.latitude},{self.longitude}"
            request_parameters = {"units": "si"}
            session = async_get_clientsession(self.hass)
            async with session.get(
                request_url,
                params=request_parameters,
                timeout=aiohttp.ClientTimeout(total=PIRATE_WEATHER_TIMEOUT),
            ) as http_response:
                if http_response.status != 200:
                    self.record_failure(f"HTTP {http_response.status}")
                    return False
                response_data = await http_response.json()
            hourly_entries = response_data.get("hourly", {}).get("data", [])
            fetched_at = dt_util.now().isoformat()
            saved_count = 0
            for hour_entry in hourly_entries:
                timestamp = hour_entry.get("time")
                if not timestamp:
                    continue
                forecast_datetime = datetime.fromtimestamp(timestamp)
                date_key = forecast_datetime.strftime("%Y-%m-%d")
                forecast_hour = forecast_datetime.hour
                cloud_percent = hour_entry.get("cloudCover")
                if cloud_percent is not None:
                    cloud_cover_percent = float(cloud_percent * 100)
                    await self._save_to_db(
                        date_key, forecast_hour, cloud_cover_percent, fetched_at
                    )
                    saved_count += 1
            self._last_fetch_time = dt_util.now()
            self.reset_failures()
            _LOGGER.debug("PirateWeather: Saved %d hours to DB", saved_count)
        except Exception as expert_error:
            self.record_failure(str(expert_error))
            _LOGGER.warning("PirateWeather fetch error: %s", expert_error)
            return False
        return True

    async def _save_to_db(
        self, date_str: str, hour: int, cloud_cover: float, fetched_at: str
    ) -> None:
        """Save forecast data to weather_cache_pirate_weather table. @zara"""
        try:
            await self.db.execute(
                "INSERT INTO weather_cache_pirate_weather\n                   (forecast_date, hour, cloud_cover, fetched_at)\n                   VALUES (?, ?, ?, ?)\n                   ON CONFLICT(forecast_date, hour) DO UPDATE SET\n                       cloud_cover = excluded.cloud_cover,\n                       fetched_at = excluded.fetched_at",
                (date_str, hour, cloud_cover, fetched_at),
            )
        except Exception as expert_error:
            _LOGGER.debug("PirateWeather DB save error: %s", expert_error)
        return None if True else None

    async def get_cloud_cover(self, date: str, hour: int) -> Optional[float]:
        """Get cloud cover from database. @zara"""
        if not self._enabled:
            return
        try:
            row = await self.db.fetchone(
                "SELECT cloud_cover FROM weather_cache_pirate_weather\n                   WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            return row[0] if row else None
        except Exception as error:
            _LOGGER.debug("PirateWeather DB read error: %s", error)
            return

    async def get_forecast_data(self, date: str, hour: int) -> Optional[dict[str, Any]]:
        """Get forecast data from database. @zara"""
        cloud_percent = await self.get_cloud_cover(date, hour)
        if cloud_percent is not None:
            return {"cloud_cover": cloud_percent}


class WttrInExpert(WeatherExpert):
    """Expert using wttr.in (World Weather Online) data with DB persistence. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        db_manager: DatabaseManager,
        latitude: float,
        longitude: float,
    ):
        super().__init__("wttr_in")
        self.hass = hass
        self.db = db_manager
        self.latitude = latitude
        self.longitude = longitude
        self._last_fetch_time = None
        self._cache_max_age_hours = 6

    async def _check_db_freshness(self) -> bool:
        """Check if DB has fresh data for today. @zara"""
        try:
            today_iso = dt_util.now().strftime("%Y-%m-%d")
            row = await self.db.fetchone(
                "SELECT MAX(fetched_at), COUNT(*) FROM weather_cache_wttr_in\n                   WHERE forecast_date = ?",
                (today_iso,),
            )
            if not row or not row[0] or row[1] < 8:
                return False
            fetched_at = datetime.fromisoformat(row[0])
            age_hours = (dt_util.now() - fetched_at).total_seconds() / 3600
            return age_hours < self._cache_max_age_hours
        except Exception:
            return False

    async def _fetch_forecast(self, force: bool = False) -> bool:
        """Fetch forecast from wttr.in API and persist to DB. @zara"""
        if not force:
            if self._last_fetch_time:
                elapsed_fetch_seconds = (
                    dt_util.now() - self._last_fetch_time
                ).total_seconds()
                if elapsed_fetch_seconds < 21600:
                    return True
        if not force:
            database_fresh = await self._check_db_freshness()
            if database_fresh:
                _LOGGER.debug("WttrIn: Using existing DB data (still fresh)")
                self._last_fetch_time = dt_util.now()
                return True
        try:
            request_url = (
                f"{WTTR_IN_BASE_URL}/{self.latitude},{self.longitude}?format=j1"
            )
            session = async_get_clientsession(self.hass)
            async with session.get(
                request_url,
                timeout=aiohttp.ClientTimeout(total=WTTR_IN_TIMEOUT),
                headers={
                    "User-Agent": "SolarForecastEAI/1.0",
                    "Accept-Encoding": "identity",
                },
                allow_redirects=False,
                auto_decompress=False,
            ) as http_response:
                if http_response.status != 200:
                    self.record_failure(f"HTTP {http_response.status}")
                    return False
                response_payload = await _read_wttr_payload(http_response)
            validated_hours = _parse_wttr_payload(
                response_payload, dt_util.now().date()
            )
            fetched_at = dt_util.now().isoformat()
            saved_count = await self._save_rows_to_db(validated_hours, fetched_at)
            if saved_count != len(validated_hours):
                raise RuntimeError("wttr.in forecast persistence was incomplete")
            self._last_fetch_time = dt_util.now()
            self.reset_failures()
            _LOGGER.debug("WttrIn: Saved %d hours to DB", saved_count)
        except Exception as expert_error:
            error_type = type(expert_error).__name__
            self.record_failure(f"wttr.in fetch failed ({error_type})")
            _LOGGER.warning("WttrIn fetch failed (%s)", error_type)
            return False
        return True

    async def _save_rows_to_db(
        self, rows: list[tuple[str, int, dict[str, Optional[float]]]], fetched_at: str
    ) -> int:
        """Persist a validated wttr.in response as one bounded batch."""
        cache_rows = [
            (
                date_key,
                forecast_hour,
                hour_weather["cloud_cover"],
                hour_weather["temperature"],
                hour_weather["humidity"],
                hour_weather["wind_speed"],
                hour_weather["precipitation"],
                hour_weather["pressure"],
                fetched_at,
            )
            for date_key, forecast_hour, hour_weather in rows
        ]
        cache_sql = "INSERT INTO weather_cache_wttr_in\n                   (forecast_date, hour, cloud_cover, temperature, humidity,\n                    wind_speed, precipitation, pressure, fetched_at)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(forecast_date, hour) DO UPDATE SET\n                       cloud_cover = excluded.cloud_cover,\n                       temperature = excluded.temperature,\n                       humidity = excluded.humidity,\n                       wind_speed = excluded.wind_speed,\n                       precipitation = excluded.precipitation,\n                       pressure = excluded.pressure,\n                       fetched_at = excluded.fetched_at"
        async with self.db.transaction():
            for row in cache_rows:
                await self.db.execute(cache_sql, row, auto_commit=False)
        return len(cache_rows)

    async def get_cloud_cover(self, date: str, hour: int) -> Optional[float]:
        """Get cloud cover from database. @zara"""
        try:
            row = await self.db.fetchone(
                "SELECT cloud_cover FROM weather_cache_wttr_in\n                   WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            return row[0] if row else None
        except Exception as error:
            _LOGGER.debug("WttrIn DB read error: %s", error)
            return

    async def get_forecast_data(self, date: str, hour: int) -> Optional[dict[str, Any]]:
        """Get full forecast data from database. @zara"""
        try:
            row = await self.db.fetchone(
                "SELECT cloud_cover, temperature, humidity, wind_speed,\n                          precipitation, pressure\n                   FROM weather_cache_wttr_in\n                   WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            if not row:
                return
            return {
                "cloud_cover": row[0],
                "temperature": row[1],
                "humidity": row[2],
                "wind_speed": row[3],
                "precipitation": row[4],
                "pressure": row[5],
            }
        except Exception as expert_error:
            _LOGGER.debug("WttrIn DB read error: %s", expert_error)
            return


class WeatherExpertBlender(DataManagerIO):
    """Blends forecasts from multiple weather experts using learned weights. @zara

    All weights stored in and retrieved from database.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db_manager: DatabaseManager,
        latitude: float,
        longitude: float,
        pirate_weather_api_key: Optional[str] = None,
    ):
        """Initialize the weather expert blender. @zara"""
        super().__init__(hass, db_manager)
        self.latitude = latitude
        self.longitude = longitude
        self._open_meteo_expert = OpenMeteoExpert(db_manager)
        self._ecmwf_layer_expert = ECMWFLayerExpert(db_manager)
        self._bright_sky_expert = BrightSkyExpert(hass, db_manager, latitude, longitude)
        self._pirate_weather_expert = PirateWeatherExpert(
            hass, db_manager, latitude, longitude, pirate_weather_api_key
        )
        self._wttr_expert = WttrInExpert(hass, db_manager, latitude, longitude)
        self._experts = [
            self._open_meteo_expert,
            self._ecmwf_layer_expert,
            self._bright_sky_expert,
            self._pirate_weather_expert,
            self._wttr_expert,
        ]
        self._weights_cache = {}
        self._weights_loaded = False
        self._visibility_learner = None
        _LOGGER.info(
            "WeatherExpertBlender initialized with %d experts",
            len(
                [
                    expert_error
                    for expert_error in self._experts
                    if self._is_expert_enabled(expert_error)
                ]
            ),
        )

    def _is_expert_enabled(self, expert: WeatherExpert) -> bool:
        """Check if expert is enabled. @zara"""
        if isinstance(expert, PirateWeatherExpert):
            return expert._enabled
        return True

    async def async_init(self) -> bool:
        """Initialize and load weights from database. @zara"""
        await self.ensure_initialized()
        await self._load_weights_from_db()
        return True

    async def _load_weights_from_db(self) -> None:
        """Load learned weights from database. @zara"""
        try:
            validated_hours = await self.fetch_all(
                "SELECT cloud_type, expert_name, weight FROM weather_expert_weights"
            )
            self._weights_cache.clear()
            for row in validated_hours:
                cloud_category = row[0]
                expert_name = row[1]
                expert_weight = row[2]
                if cloud_category not in self._weights_cache:
                    self._weights_cache[cloud_category] = {}
                self._weights_cache[cloud_category][expert_name] = expert_weight
            for cloud_category, default_weights in DEFAULT_EXPERT_WEIGHTS.items():
                if cloud_category not in self._weights_cache:
                    self._weights_cache[cloud_category] = default_weights.copy()
                else:
                    for default_expert_key, expert_weight in default_weights.items():
                        if (
                            default_expert_key
                            not in self._weights_cache[cloud_category]
                        ):
                            self._weights_cache[cloud_category][
                                default_expert_key
                            ] = expert_weight
            self._weights_loaded = True
            _LOGGER.debug("Loaded expert weights from database")
        except Exception as expert_error:
            _LOGGER.warning("Error loading weights from database: %s", expert_error)
            self._weights_cache = {
                cloud_type: weights.copy()
                for cloud_type, weights in DEFAULT_EXPERT_WEIGHTS.items()
            }
            self._weights_loaded = True
        return None if False else None

    async def _save_weights_to_db(self) -> None:
        """Save current weights to database. @zara"""
        try:
            for cloud_category, weights_by_expert in self._weights_cache.items():
                for expert_name, expert_weight in weights_by_expert.items():
                    await self.execute_query(
                        "INSERT INTO weather_expert_weights\n                           (cloud_type, expert_name, weight, last_updated)\n                           VALUES (?, ?, ?, CURRENT_TIMESTAMP)\n                           ON CONFLICT(cloud_type, expert_name) DO UPDATE SET\n                               weight = excluded.weight,\n                               last_updated = CURRENT_TIMESTAMP",
                        (cloud_category, expert_name, expert_weight),
                    )
            _LOGGER.debug("Saved expert weights to database")
        except Exception as expert_error:
            _LOGGER.error("Error saving weights to database: %s", expert_error)
        return None if True else None

    def _get_weights_for_cloud_type(self, cloud_type: CloudType) -> dict[str, float]:
        """Get weights for specific cloud type. @zara

        V16.1: For FOG/FOG_LIGHT, uses learned visibility weights from
        VisibilityLearner if available, merged with defaults.
        """
        cloud_category_key = cloud_type.value
        if (
            cloud_type in (CloudType.FOG, CloudType.FOG_LIGHT)
            and self._visibility_learner
        ):
            try:
                learned_fog_weights = self._visibility_learner.get_fog_weight(
                    cloud_category_key
                )
                if learned_fog_weights:
                    fog_weights = DEFAULT_EXPERT_WEIGHTS.get(
                        cloud_category_key, {}
                    ).copy()
                    if "bright_sky" in learned_fog_weights:
                        fog_weights["bright_sky"] = learned_fog_weights["bright_sky"]
                    if "pirate_weather" in learned_fog_weights:
                        fog_weights["pirate_weather"] = learned_fog_weights[
                            "pirate_weather"
                        ]
                    forecast_weight_scale = 0.3
                    for forecast_expert_key in (
                        "open_meteo",
                        "wttr_in",
                        "ecmwf_layers",
                    ):
                        if forecast_expert_key in fog_weights:
                            fog_weights[forecast_expert_key] *= forecast_weight_scale
                    total_weight = sum(fog_weights.values())
                    if total_weight > 0:
                        fog_weights = {
                            weight_key: weight_value / total_weight
                            for weight_key, weight_value in fog_weights.items()
                        }
                    return fog_weights
            except Exception as expert_error:
                _LOGGER.debug(
                    "Error getting visibility weights for %s: %s",
                    cloud_category_key,
                    expert_error,
                )
        return self._weights_cache.get(
            cloud_category_key, DEFAULT_EXPERT_WEIGHTS.get(cloud_category_key, {})
        )

    async def get_blended_cloud_cover(
        self,
        date: str,
        hour: int,
        cloud_low: Optional[float] = None,
        cloud_mid: Optional[float] = None,
        cloud_high: Optional[float] = None,
        fallback_cloud: Optional[float] = None,
        visibility_m: Optional[float] = None,
    ) -> tuple[float, dict[str, Any]]:
        "Get weighted blended cloud cover from all experts. @zara\n\nArgs:\n    date: Date string (YYYY-MM-DD)\n    hour: Hour (0-23)\n    cloud_low: Low cloud cover (optional)\n    cloud_mid: Mid cloud cover (optional)\n    cloud_high: High cloud cover (optional)\n    fallback_cloud: Fallback cloud cover value\n    visibility_m: Visibility in meters (optional, for fog detection)\n\nReturns:\n    Tuple of (blended_cloud_cover, blend_info_dict)\n"
        if not self._weights_loaded:
            await self._load_weights_from_db()
        cloud_category = classify_cloud_type(
            cloud_low, cloud_mid, cloud_high, fallback_cloud, visibility_m
        )
        category_weights = self._get_weights_for_cloud_type(cloud_category)
        expert_cloud_values = {}
        for expert in self._experts:
            if not self._is_expert_enabled(expert):
                continue
            try:
                cloud_percent = await expert.get_cloud_cover(date, hour)
                if cloud_percent is not None:
                    expert_cloud_values[expert.name] = cloud_percent
            except Exception as expert_error:
                _LOGGER.debug("Expert %s error: %s", expert.name, expert_error)
                continue
        if not expert_cloud_values:
            fallback_cloud_percent = (
                fallback_cloud if fallback_cloud is not None else 50.0
            )
            return (
                fallback_cloud_percent,
                {
                    "cloud_type": cloud_category.value,
                    "experts": {},
                    "weights_used": {},
                    "fallback": True,
                },
            )
        available_weight = 0.0
        weighted_cloud_sum = 0.0
        weights_used = {}
        for expert_name, expert_cloud_percent in expert_cloud_values.items():
            expert_weight = category_weights.get(expert_name, 0.0)
            if expert_weight > 0:
                weighted_cloud_sum += expert_cloud_percent * expert_weight
                available_weight += expert_weight
                weights_used[expert_name] = expert_weight
        if available_weight > 0:
            blended_cloud_percent = weighted_cloud_sum / available_weight
        else:
            blended_cloud_percent = sum(expert_cloud_values.values()) / len(
                expert_cloud_values
            )
        blended_cloud_percent = max(0.0, min(100.0, blended_cloud_percent))
        blend_info = {
            "cloud_type": cloud_category.value,
            "experts": expert_cloud_values,
            "weights_used": weights_used,
            "total_weight": available_weight,
            "blended": round(blended_cloud_percent, 1),
            "visibility_m": visibility_m,
        }
        return (round(blended_cloud_percent, 1), blend_info)

    async def get_dual_path_blend(
        self,
        date: str,
        hour: int,
        cloud_low: Optional[float] = None,
        cloud_mid: Optional[float] = None,
        cloud_high: Optional[float] = None,
        fallback_cloud: Optional[float] = None,
        path_a_weight: float = 0.5,
    ) -> tuple[float, dict[str, Any]]:
        "Get dual-path blend combining cloud% and transmission approaches. @zara\n\nPath A: Weighted cloud cover blending\nPath B: Physics-based transmission blending\n\nReturns:\n    Tuple of (transmission_percent, dual_info_dict)\n"
        blended_cloud_percent, blend_details = await self.get_blended_cloud_cover(
            date, hour, cloud_low, cloud_mid, cloud_high, fallback_cloud
        )
        cloud_category = classify_cloud_type(
            cloud_low, cloud_mid, cloud_high, fallback_cloud
        )
        path_b_transmission = cloud_to_transmission(
            blended_cloud_percent, cloud_category
        )
        path_a_transmission = 100.0 - blended_cloud_percent
        path_b_weight = 1.0 - path_a_weight
        final_transmission = (
            path_a_transmission * path_a_weight + path_b_transmission * path_b_weight
        )
        dual_path_details = {
            **blend_details,
            "path_a_transmission": round(path_a_transmission, 1),
            "path_b_transmission": round(path_b_transmission, 1),
            "path_a_weight": path_a_weight,
            "final_transmission": round(final_transmission, 1),
            "final_cloud": round(100.0 - final_transmission, 1),
        }
        return (round(final_transmission, 1), dual_path_details)

    async def update_expert_weight(
        self, cloud_type: CloudType, expert_name: str, mae: float
    ) -> None:
        """Update expert weight based on MAE using smoothed learning. @zara"""
        if mae < LEARNING_MIN_ERROR:
            return
        cloud_category_key = cloud_type.value
        if cloud_category_key not in self._weights_cache:
            self._weights_cache[cloud_category_key] = DEFAULT_EXPERT_WEIGHTS.get(
                cloud_category_key, {}
            ).copy()
        if expert_name not in self._weights_cache[cloud_category_key]:
            return
        previous_weight = self._weights_cache[cloud_category_key][expert_name]
        accuracy_weight = 1.0 / (1.0 + mae / 100.0)
        target_weight = accuracy_weight
        updated_weight = (
            previous_weight * (1 - LEARNING_SMOOTHING_FACTOR)
            + target_weight * LEARNING_SMOOTHING_FACTOR
        )
        updated_weight = max(0.05, min(0.95, updated_weight))
        self._weights_cache[cloud_category_key][expert_name] = updated_weight
        total_weight = sum(self._weights_cache[cloud_category_key].values())
        if total_weight > 0:
            for weight_key in self._weights_cache[cloud_category_key]:
                self._weights_cache[cloud_category_key][weight_key] /= total_weight
        await self._save_weights_to_db()

    def get_blend_stats(self) -> dict[str, Any]:
        """Get blending statistics. @zara"""
        return {
            "experts_enabled": [
                expert_error.name
                for expert_error in self._experts
                if self._is_expert_enabled(expert_error)
            ],
            "weights_loaded": self._weights_loaded,
            "cloud_types_configured": list(self._weights_cache.keys()),
        }

    async def get_expert_db_status(self) -> dict[str, Any]:
        """Get database status for all experts with data counts. @zara"""
        today_iso = dt_util.now().strftime("%Y-%m-%d")
        db_status = {}
        try:
            row = await self.fetch_one(
                "SELECT COUNT(*), MAX(fetched_at) FROM weather_cache_bright_sky\n                   WHERE forecast_date >= ?",
                (today_iso,),
            )
            db_status["bright_sky"] = {
                "hours_in_db": row[0] if row else 0,
                "last_fetched": row[1] if row else None,
                "healthy": self._bright_sky_expert.is_healthy,
            }
        except Exception as expert_error:
            db_status["bright_sky"] = {"error": str(expert_error)}
        try:
            row = await self.fetch_one(
                "SELECT COUNT(*), MAX(fetched_at) FROM weather_cache_wttr_in\n                   WHERE forecast_date >= ?",
                (today_iso,),
            )
            db_status["wttr_in"] = {
                "hours_in_db": row[0] if row else 0,
                "last_fetched": row[1] if row else None,
                "healthy": self._wttr_expert.is_healthy,
            }
        except Exception as expert_error:
            db_status["wttr_in"] = {"error": str(expert_error)}
        try:
            if self._pirate_weather_expert._enabled:
                row = await self.fetch_one(
                    "SELECT COUNT(*), MAX(fetched_at) FROM weather_cache_pirate_weather\n                       WHERE forecast_date >= ?",
                    (today_iso,),
                )
                db_status["pirate_weather"] = {
                    "hours_in_db": row[0] if row else 0,
                    "last_fetched": row[1] if row else None,
                    "healthy": self._pirate_weather_expert.is_healthy,
                    "enabled": True,
                }
            else:
                db_status["pirate_weather"] = {"enabled": False, "reason": "no_api_key"}
        except Exception as expert_error:
            db_status["pirate_weather"] = {"error": str(expert_error)}
        try:
            row = await self.fetch_one(
                "SELECT COUNT(*) FROM weather_forecast WHERE forecast_date >= ?",
                (today_iso,),
            )
            db_status["open_meteo"] = {
                "hours_in_db": row[0] if row else 0,
                "source": "weather_forecast table",
            }
        except Exception as expert_error:
            db_status["open_meteo"] = {"error": str(expert_error)}
        return db_status if True else None

    def set_visibility_learner(self, learner) -> None:
        "Set the VisibilityLearner reference for FOG/FOG_LIGHT weight learning. @zara V16.1\n\nThis allows the blender to use learned visibility weights\ninstead of static defaults for fog conditions.\n\nArgs:\n    learner: VisibilityLearner instance\n"
        self._visibility_learner = learner
        _LOGGER.debug("VisibilityLearner connected to WeatherExpertBlender")


class WeatherExpertLearner(DataManagerIO):
    """Learns expert weights from actual IST (Irradiance-Sunshine-Time) data. @zara

    Compares forecasts vs actuals and updates expert weights accordingly.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db_manager: DatabaseManager,
        blender: WeatherExpertBlender,
        config_entry=None,
    ):
        """Initialize the learner. @zara"""
        super().__init__(hass, db_manager)
        self._blender = blender
        self._config_entry = config_entry
        self._visibility_learner = None
        _LOGGER.info("WeatherExpertLearner initialized")

    async def learn_from_day(self, date_str: str) -> bool:
        """Learn expert weights from a day's data - PER CLOUD TYPE. @zara V16.1

        Compares forecasted cloud cover vs actual measured cloud cover
        and updates expert weights based on accuracy FOR EACH CLOUD TYPE.

        FIX: Now learns separately for each cloud type bucket instead of
        only the dominant type for the day.

        Args:
            date_str: Date to learn from (YYYY-MM-DD)

        Returns:
            True if learning was successful
        """
        try:
            actual_cloud_rows = await self.fetch_all(
                "SELECT hour, cloud_cover_percent\n                   FROM hourly_weather_actual\n                   WHERE date = ? AND cloud_cover_percent IS NOT NULL",
                (date_str,),
            )
            if len(actual_cloud_rows) < LEARNING_MIN_HOURS:
                _LOGGER.debug(
                    "Not enough data for learning: %d hours (need %d)",
                    len(actual_cloud_rows),
                    LEARNING_MIN_HOURS,
                )
                return False
            errors_by_category = {}
            hours_by_category = {}
            for actual_row in actual_cloud_rows:
                forecast_hour = actual_row[0]
                actual_cloud_percent = actual_row[1]
                forecast_data = (
                    await self._blender._open_meteo_expert.get_forecast_data(
                        date_str, forecast_hour
                    )
                )
                if not forecast_data:
                    continue
                cloud_category = classify_cloud_type(
                    forecast_data.get("cloud_cover_low"),
                    forecast_data.get("cloud_cover_mid"),
                    forecast_data.get("cloud_cover_high"),
                    forecast_data.get("cloud_cover"),
                    forecast_data.get("visibility_m"),
                )
                cloud_category_key = cloud_category.value
                hours_by_category[cloud_category_key] = (
                    hours_by_category.get(cloud_category_key, 0) + 1
                )
                if cloud_category_key not in errors_by_category:
                    errors_by_category[cloud_category_key] = {}
                for expert in self._blender._experts:
                    if not self._blender._is_expert_enabled(expert):
                        continue
                    try:
                        expert_cloud_percent = await expert.get_cloud_cover(
                            date_str, forecast_hour
                        )
                        if expert_cloud_percent is not None:
                            absolute_error = abs(
                                expert_cloud_percent - actual_cloud_percent
                            )
                            if (
                                expert.name
                                not in errors_by_category[cloud_category_key]
                            ):
                                errors_by_category[cloud_category_key][expert.name] = []
                            errors_by_category[cloud_category_key][expert.name].append(
                                absolute_error
                            )
                    except Exception:
                        continue
                continue
            updated_experts = 0
            for cloud_category_key, expert_errors in errors_by_category.items():
                category_hours = hours_by_category.get(cloud_category_key, 0)
                if category_hours < 2:
                    continue
                cloud_category = CloudType(cloud_category_key)
                for expert_name, error_samples in expert_errors.items():
                    if len(error_samples) < 2:
                        continue
                    mean_absolute_error = sum(error_samples) / len(error_samples)
                    await self._blender.update_expert_weight(
                        cloud_category, expert_name, mean_absolute_error
                    )
                    await self._save_learning_record(
                        date_str, cloud_category_key, expert_name, mean_absolute_error
                    )
                updated_experts += 1
            _LOGGER.info(
                "Learned from %s: %d hours across %d cloud types (buckets: %s)",
                date_str,
                len(actual_cloud_rows),
                updated_experts,
                ", ".join(
                    (
                        f"{weight_key}:{weight_value}h"
                        for weight_key, weight_value in hours_by_category.items()
                    )
                ),
            )
            if self._visibility_learner:
                try:
                    visibility_learning = await self._visibility_learner.learn_from_day(
                        date_str
                    )
                    if visibility_learning and visibility_learning.get("success"):
                        fog_hours = visibility_learning.get("fog_hours_detected", 0)
                        light_fog_hours = visibility_learning.get(
                            "fog_light_hours_detected", 0
                        )
                        if fog_hours > 0 or light_fog_hours > 0:
                            _LOGGER.info(
                                "Visibility learning for %s: %d fog, %d fog_light hours",
                                date_str,
                                fog_hours,
                                light_fog_hours,
                            )
                except Exception as visibility_error:
                    _LOGGER.warning("Visibility learning error: %s", visibility_error)
            if True:
                return True
        except Exception as expert_error:
            _LOGGER.error("Error learning from day %s: %s", date_str, expert_error)
            return False

    async def _save_learning_record(
        self, date_str: str, cloud_type: str, expert_name: str, mae: float
    ) -> None:
        """Save learning record to database. @zara"""
        try:
            category_weights = self._blender._weights_cache.get(cloud_type, {})
            expert_weight = category_weights.get(expert_name, 0.2)
            await self.execute_query(
                "INSERT INTO weather_expert_learning\n                   (date, cloud_type, expert_name, mae, weight_after, learned_at)\n                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n                   ON CONFLICT(date, cloud_type, expert_name) DO UPDATE SET\n                       mae = excluded.mae,\n                       weight_after = excluded.weight_after,\n                       learned_at = CURRENT_TIMESTAMP",
                (date_str, cloud_type, expert_name, mae, expert_weight),
            )
        except Exception as expert_error:
            _LOGGER.error("Error saving learning record: %s", expert_error)
        return None if True else None

    def set_visibility_learner(self, learner) -> None:
        "Set the VisibilityLearner reference for FOG/FOG_LIGHT weight learning. @zara V16.1\n\nThis enables automatic visibility learning when learn_from_day is called.\n\nArgs:\n    learner: VisibilityLearner instance\n"
        self._visibility_learner = learner
        _LOGGER.debug("VisibilityLearner connected to WeatherExpertLearner")

    async def learn_snow_prediction_accuracy(
        self,
        date_str: str,
        hourly_predictions: list[dict[str, Any]],
        hourly_actuals: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        'Learn snow prediction accuracy from actual observations (DB-based). @zara\n\nCompares API-predicted snow with actual production to validate snow predictions.\nFor users WITHOUT rain sensors, we learn by comparing:\n- API predicted snow (condition contains "snow" OR precip > 0 + temp < 2°C)\n- Actual production = 0 or near 0 despite expected radiation > 100 W/m²\n\nArgs:\n    date_str: Date to analyze (YYYY-MM-DD)\n    hourly_predictions: List of hourly predictions with actual_kwh\n    hourly_actuals: Dict of hourly actual weather data\n\nReturns:\n    Dict with snow prediction analysis results\n'
        snow_learning_result = {
            "success": False,
            "date": date_str,
            "snow_predictions": [],
            "total_snow_predicted": 0,
            "snow_confirmed": 0,
            "accuracy": 0.0,
        }
        try:
            minimum_radiation_wm2 = 100
            maximum_actual_kwh = 0.02
            snow_predictions = []
            weather_forecast_rows = await self.fetch_all(
                "SELECT hour, temperature, rain, solar_radiation_wm2\n                   FROM weather_forecast\n                   WHERE forecast_date = ?",
                (date_str,),
            )
            weather_by_hour = {}
            for row in weather_forecast_rows:
                forecast_hour = row[0]
                weather_by_hour[forecast_hour] = {
                    "temperature": row[1],
                    "precipitation_mm": row[2] or 0,
                    "solar_radiation_wm2": row[3] or 0,
                }
            for hourly_prediction in hourly_predictions:
                forecast_hour = hourly_prediction.get("target_hour")
                if forecast_hour is None:
                    continue
                hourly_weather = weather_by_hour.get(forecast_hour, {})
                if not hourly_weather:
                    continue
                temperature_c = hourly_weather.get("temperature", 10)
                precipitation_mm = hourly_weather.get("precipitation_mm", 0)
                expected_radiation_wm2 = hourly_weather.get("solar_radiation_wm2", 0)
                actual_weather = hourly_actuals.get(str(forecast_hour), {})
                condition = actual_weather.get("condition", "").lower()
                snow_expected = "snow" in condition or (
                    precipitation_mm > 0.5 and temperature_c < 2.0
                )
                if not snow_expected:
                    continue
                if expected_radiation_wm2 < minimum_radiation_wm2:
                    continue
                actual_kwh = hourly_prediction.get("actual_kwh", 0) or 0
                snow_confirmed = actual_kwh < maximum_actual_kwh or actual_weather.get(
                    "snow_covered_panels", False
                )
                snow_predictions.append(
                    {
                        "hour": forecast_hour,
                        "predicted": True,
                        "confirmed": snow_confirmed,
                        "expected_radiation": expected_radiation_wm2,
                        "actual_production": actual_kwh,
                        "condition": condition,
                        "temp": temperature_c,
                        "precip": precipitation_mm,
                    }
                )
                continue
            snow_learning_result["snow_predictions"] = snow_predictions
            snow_learning_result["total_snow_predicted"] = len(snow_predictions)
            if snow_predictions:
                confirmed_count = sum(
                    (
                        1
                        for snow_prediction in snow_predictions
                        if snow_prediction["confirmed"]
                    )
                )
                snow_prediction_count = len(snow_predictions)
                snow_accuracy = (
                    confirmed_count / snow_prediction_count
                    if snow_prediction_count > 0
                    else 0.0
                )
                snow_learning_result["snow_confirmed"] = confirmed_count
                snow_learning_result["accuracy"] = round(snow_accuracy, 3)
                await self._update_snow_stats(confirmed_count, snow_prediction_count)
                snow_learning_result["success"] = True
                _LOGGER.debug(
                    "Snow prediction learning for %s: %d/%d confirmed (%.1f%%)",
                    date_str,
                    confirmed_count,
                    snow_prediction_count,
                    snow_accuracy * 100,
                )
            else:
                snow_learning_result["success"] = False
                _LOGGER.debug("No snow predictions for %s", date_str)
            return snow_learning_result if True else None
        except Exception as expert_error:
            _LOGGER.error(
                "Error in snow prediction learning for %s: %s", date_str, expert_error
            )
            return snow_learning_result if True else None

    async def _update_snow_stats(self, correct: int, total: int) -> None:
        """Update global snow prediction statistics in DB. @zara"""
        try:
            row = await self.fetch_one(
                "SELECT total_predictions, correct_predictions FROM weather_expert_snow_stats WHERE id = 1"
            )
            if row:
                previous_total = row[0] or 0
                previous_correct = row[1] or 0
            else:
                previous_total = 0
                previous_correct = 0
            new_total = previous_total + total
            new_correct = previous_correct + correct
            overall_accuracy = new_correct / new_total if new_total > 0 else 0.0
            await self.execute_query(
                "INSERT INTO weather_expert_snow_stats (id, total_predictions, correct_predictions, accuracy, last_updated)\n                   VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)\n                   ON CONFLICT(id) DO UPDATE SET\n                       total_predictions = excluded.total_predictions,\n                       correct_predictions = excluded.correct_predictions,\n                       accuracy = excluded.accuracy,\n                       last_updated = CURRENT_TIMESTAMP",
                (new_total, new_correct, overall_accuracy),
            )
            _LOGGER.debug(
                "Snow stats updated: %d/%d correct (%.1f%% accuracy)",
                new_correct,
                new_total,
                overall_accuracy * 100,
            )
        except Exception as expert_error:
            _LOGGER.error("Error updating snow stats: %s", expert_error)
        return None if True else None
