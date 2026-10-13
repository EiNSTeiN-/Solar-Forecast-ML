# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Weather Expert Blender - Multi-Source Weather Blending with Learned Weights.

This module implements a multi-expert weather blending system that:
1. Treats each weather source as an independent "expert"
2. Learns accuracy weights per cloud type from actual observations
3. Blends forecasts using cloud-type-specific weights

Architecture:
    [Open-Meteo API] ──► OpenMeteoExpert    ──┐
                                              │
    [wttr.in API]    ──► WttrInExpert       ──┤
                                              │
    [Bright Sky]     ──► BrightSkyExpert    ──┼──► WeatherExpertBlender
                                              │         │
    [Pirate Weather] ──► PirateWeatherExpert──┘         ▼
                                                  Blended Forecast

@zara
"""
from __future__ import annotations
import asyncio
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import aiohttp
from homeassistant.core import HomeAssistant
from .blend_math import circular_weighted_average, km_to_meters, scale_or_none
from .cloud_type import classify_cloud_type as classify_cloud_type_value
from .const import (
    BRIGHT_SKY_UPDATE_HOURS,
    EXPERT_WEIGHT_KEYS,
    OPEN_METEO_UPDATE_HOURS,
    PIRATE_WEATHER_UPDATE_HOURS,
    WTTR_UPDATE_HOURS,
)
from .file_io import async_write_json

_LOGGER = logging.getLogger(__name__)


class CloudType(Enum):
    """Cloud type classification."""

    CLEAR = "clear"
    CIRRUS = "cirrus"
    FAIR = "fair"
    MIXED = "mixed"
    STRATUS = "stratus"
    OVERCAST = "overcast"


DEFAULT_EXPERT_WEIGHTS: Dict[str, Dict[str, float]] = {
    CloudType.CLEAR.value: {
        "open_meteo": 0.2,
        "wttr_in": 0.15,
        "bright_sky": 0.4,
        "pirate_weather": 0.25,
    },
    CloudType.CIRRUS.value: {
        "open_meteo": 0.2,
        "wttr_in": 0.15,
        "bright_sky": 0.35,
        "pirate_weather": 0.3,
    },
    CloudType.FAIR.value: {
        "open_meteo": 0.2,
        "wttr_in": 0.15,
        "bright_sky": 0.4,
        "pirate_weather": 0.25,
    },
    CloudType.MIXED.value: {
        "open_meteo": 0.2,
        "wttr_in": 0.15,
        "bright_sky": 0.35,
        "pirate_weather": 0.3,
    },
    CloudType.STRATUS.value: {
        "open_meteo": 0.15,
        "wttr_in": 0.15,
        "bright_sky": 0.4,
        "pirate_weather": 0.3,
    },
    CloudType.OVERCAST.value: {
        "open_meteo": 0.15,
        "wttr_in": 0.15,
        "bright_sky": 0.4,
        "pirate_weather": 0.3,
    },
}
OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"
BRIGHT_SKY_BASE_URL = "https://api.brightsky.dev/weather"
WTTR_IN_BASE_URL = "https://wttr.in"
PIRATE_WEATHER_BASE_URL = "https://api.pirateweather.net/forecast"
API_TIMEOUT = 15
CACHE_RETENTION_DAYS = 730
FAILURE_COOLDOWN_SECONDS = 300


class WeatherExpert(ABC):
    """Abstract base class for weather data experts."""

    def __init__(self, name: str, weight_key: str, time_zone: str = "UTC"):
        self.name = name
        self.weight_key = weight_key
        self.set_time_zone(time_zone)
        self._last_error = None
        self._enabled = True
        self._last_failure_time = None
        self._http_session = None

    def set_http_session(self, session: aiohttp.ClientSession) -> None:
        """Use the shared Home Assistant client session."""
        self._http_session = session

    def set_time_zone(self, time_zone: str) -> None:
        """Use the Home Assistant timezone for all provider hour keys."""
        try:
            self._time_zone = ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            self._time_zone = ZoneInfo("UTC")
        return None if True else None

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    def _stored_timestamp(self, value: str) -> datetime:
        """Normalize current and legacy cache metadata to aware UTC."""
        parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed_datetime.tzinfo is None:
            parsed_datetime = parsed_datetime.replace(tzinfo=self._time_zone)
        return parsed_datetime.astimezone(timezone.utc)

    @abstractmethod
    async def fetch_forecast(self, latitude: float, longitude: float) -> bool:
        """Fetch forecast from API."""

    @abstractmethod
    async def get_weather(self, date: str, hour: int) -> Optional[Dict[str, Any]]:
        """Get weather data for a specific hour."""

    def is_enabled(self) -> bool:
        return self._enabled

    def _is_in_failure_cooldown(self) -> bool:
        """Check if we're still in cooldown after a failed fetch."""
        if not self._last_failure_time:
            return False
        failure_age_seconds = (
            self._utc_now() - self._last_failure_time
        ).total_seconds()
        return failure_age_seconds < FAILURE_COOLDOWN_SECONDS

    def _mark_failure(self) -> None:
        """Mark a fetch failure to prevent rapid retries."""
        self._last_failure_time = self._utc_now()

    async def _get_json(
        self, url: str, headers: Optional[Dict[str, str]] = None
    ) -> tuple[Optional[Dict[str, Any]], Optional[int]]:
        """GET JSON using the shared session, with cooldown on HTTP and timeout errors."""
        request_headers = headers or {"User-Agent": "WeatherFusionAI/1.0"}
        session = self._http_session
        owns_session = False
        if session is None:
            session = aiohttp.ClientSession()
            owns_session = True
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                headers=request_headers,
            ) as response:
                if response.status != 200:
                    self._last_error = f"HTTP {response.status}"
                    self._mark_failure()
                    return (None, response.status)
                return (await response.json(), response.status)
        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            self._last_error = str(error) or type(error).__name__
            self._mark_failure()
            return (None, None)
        finally:
            while True:
                if owns_session:
                    await session.close()
                break
        pass
        pass
        return


class OpenMeteoExpert(WeatherExpert):
    """Expert using Open-Meteo free weather API."""

    CACHE_HOURS = OPEN_METEO_UPDATE_HOURS

    def __init__(self, cache_file: Optional[Path] = None, time_zone: str = "UTC"):
        super().__init__("GFS (NOAA)", "open_meteo", time_zone)
        self._cache = {}
        self._cache_file = cache_file
        self._cache_time = None

    async def fetch_forecast(self, latitude: float, longitude: float) -> bool:
        """Fetch forecast from Open-Meteo API."""
        if self._is_cache_valid():
            return True
        if self._is_in_failure_cooldown():
            return False
        try:
            request_url = f"{OPEN_METEO_BASE_URL}?latitude={latitude}&longitude={longitude}&hourly=temperature_2m,relative_humidity_2m,precipitation,weather_code,cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high,wind_speed_10m,wind_direction_10m,pressure_msl,visibility,uv_index,apparent_temperature,dew_point_2m,precipitation_probability&forecast_days=7&timezone=UTC"
            response_data, response_status = await self._get_json(request_url)
            if response_data is None:
                return False
            hourly_data = response_data.get("hourly", {})
            timestamps = hourly_data.get("time", [])
            self._cache.clear()
            for hour_index, timestamp in enumerate(timestamps):
                local_datetime = (
                    datetime.fromisoformat(timestamp)
                    .replace(tzinfo=timezone.utc)
                    .astimezone(self._time_zone)
                )
                date_key = local_datetime.strftime("%Y-%m-%d")
                local_hour = local_datetime.hour
                cache_key = f"{date_key}_{local_hour}"
                self._cache[cache_key] = {
                    "temperature": (
                        hourly_data.get("temperature_2m", [None])[hour_index]
                        if hour_index < len(hourly_data.get("temperature_2m", []))
                        else None
                    ),
                    "humidity": (
                        hourly_data.get("relative_humidity_2m", [None])[hour_index]
                        if hour_index < len(hourly_data.get("relative_humidity_2m", []))
                        else None
                    ),
                    "precipitation": (
                        hourly_data.get("precipitation", [None])[hour_index]
                        if hour_index < len(hourly_data.get("precipitation", []))
                        else None
                    ),
                    "weather_code": (
                        hourly_data.get("weather_code", [0])[hour_index]
                        if hour_index < len(hourly_data.get("weather_code", []))
                        else 0
                    ),
                    "cloud_cover": (
                        hourly_data.get("cloud_cover", [None])[hour_index]
                        if hour_index < len(hourly_data.get("cloud_cover", []))
                        else None
                    ),
                    "cloud_cover_low": (
                        hourly_data.get("cloud_cover_low", [None])[hour_index]
                        if hour_index < len(hourly_data.get("cloud_cover_low", []))
                        else None
                    ),
                    "cloud_cover_mid": (
                        hourly_data.get("cloud_cover_mid", [None])[hour_index]
                        if hour_index < len(hourly_data.get("cloud_cover_mid", []))
                        else None
                    ),
                    "cloud_cover_high": (
                        hourly_data.get("cloud_cover_high", [None])[hour_index]
                        if hour_index < len(hourly_data.get("cloud_cover_high", []))
                        else None
                    ),
                    "wind_speed": (
                        hourly_data.get("wind_speed_10m", [None])[hour_index]
                        if hour_index < len(hourly_data.get("wind_speed_10m", []))
                        else None
                    ),
                    "wind_direction": (
                        hourly_data.get("wind_direction_10m", [None])[hour_index]
                        if hour_index < len(hourly_data.get("wind_direction_10m", []))
                        else None
                    ),
                    "pressure": (
                        hourly_data.get("pressure_msl", [None])[hour_index]
                        if hour_index < len(hourly_data.get("pressure_msl", []))
                        else None
                    ),
                    "visibility": (
                        hourly_data.get("visibility", [None])[hour_index]
                        if hour_index < len(hourly_data.get("visibility", []))
                        else None
                    ),
                    "uv_index": (
                        hourly_data.get("uv_index", [None])[hour_index]
                        if hour_index < len(hourly_data.get("uv_index", []))
                        else None
                    ),
                    "apparent_temperature": (
                        hourly_data.get("apparent_temperature", [None])[hour_index]
                        if hour_index < len(hourly_data.get("apparent_temperature", []))
                        else None
                    ),
                    "dew_point": (
                        hourly_data.get("dew_point_2m", [None])[hour_index]
                        if hour_index < len(hourly_data.get("dew_point_2m", []))
                        else None
                    ),
                    "precipitation_probability": (
                        hourly_data.get("precipitation_probability", [None])[hour_index]
                        if hour_index
                        < len(hourly_data.get("precipitation_probability", []))
                        else None
                    ),
                }
                continue
            self._cache_time = self._utc_now()
            await self._save_cache()
            _LOGGER.debug(f"Open-Meteo: Fetched {len(self._cache)} hours")
        except Exception as error:
            self._last_error = str(error)
            self._mark_failure()
            _LOGGER.debug(f"Open-Meteo API error: {error}")
            return False
        return True

    async def get_weather(self, date: str, hour: int) -> Optional[Dict[str, Any]]:
        cache_key = f"{date}_{hour}"
        return self._cache.get(cache_key)

    def _is_cache_valid(self) -> bool:
        if not self._cache_time:
            return False
        failure_age_seconds = (self._utc_now() - self._cache_time).total_seconds()
        return failure_age_seconds < self.CACHE_HOURS * 3600

    async def _save_cache(self) -> None:
        if not self._cache_file:
            return
        try:
            response_data = {
                "version": "1.0",
                "source": "open_meteo",
                "metadata": {"last_updated": self._utc_now().isoformat()},
                "forecast": self._cache,
            }
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            await async_write_json(self._cache_file, response_data)
        except Exception as error:
            _LOGGER.warning(f"Open-Meteo cache save error: {error}")
        return None if True else None

    async def load_cache(self) -> bool:
        if not self._cache_file or not self._cache_file.exists():
            return False
        try:
            response_data = await asyncio.to_thread(
                lambda: json.loads(self._cache_file.read_text())
            )
            self._cache = response_data.get("forecast", {})
            cache_last_updated = response_data.get("metadata", {}).get("last_updated")
            if cache_last_updated:
                self._cache_time = self._stored_timestamp(cache_last_updated)
        except Exception as error:
            _LOGGER.warning(f"Open-Meteo cache load error: {error}")
            return False
        return True


class BrightSkyExpert(WeatherExpert):
    """Expert using DWD ICON model via Bright Sky API."""

    CACHE_HOURS = BRIGHT_SKY_UPDATE_HOURS

    def __init__(self, cache_file: Optional[Path] = None, time_zone: str = "UTC"):
        super().__init__("ICON (DWD)", "bright_sky", time_zone)
        self._cache = {}
        self._cache_file = cache_file
        self._cache_time = None

    async def fetch_forecast(self, latitude: float, longitude: float) -> bool:
        if self._is_cache_valid():
            return True
        if self._is_in_failure_cooldown():
            return False
        try:
            today_iso = datetime.now(self._time_zone).strftime("%Y-%m-%d")
            last_date_iso = (
                datetime.now(self._time_zone) + timedelta(days=3)
            ).strftime("%Y-%m-%d")
            request_url = f"{BRIGHT_SKY_BASE_URL}?lat={latitude}&lon={longitude}&date={today_iso}&last_date={last_date_iso}"
            response_data, response_status = await self._get_json(request_url)
            if response_data is None:
                return False
            weather_entries = response_data.get("weather", [])
            self._cache.clear()
            for weather_entry in weather_entries:
                timestamp = weather_entry.get("timestamp")
                if not timestamp:
                    continue
                local_datetime = datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                ).astimezone(self._time_zone)
                date_key = local_datetime.strftime("%Y-%m-%d")
                local_hour = local_datetime.hour
                cache_key = f"{date_key}_{local_hour}"
                self._cache[cache_key] = {
                    "temperature": weather_entry.get("temperature"),
                    "humidity": weather_entry.get("relative_humidity"),
                    "precipitation": weather_entry.get("precipitation"),
                    "cloud_cover": weather_entry.get("cloud_cover"),
                    "wind_speed": weather_entry.get("wind_speed"),
                    "wind_direction": weather_entry.get("wind_direction"),
                    "pressure": weather_entry.get("pressure_msl"),
                    "visibility": weather_entry.get("visibility"),
                    "condition": weather_entry.get("condition"),
                }
                continue
            self._cache_time = self._utc_now()
            await self._save_cache()
            _LOGGER.debug(f"Bright Sky: Fetched {len(self._cache)} hours")
        except Exception as error:
            self._last_error = str(error)
            self._mark_failure()
            _LOGGER.debug(f"Bright Sky API error: {error}")
            return False
        return True

    async def get_weather(self, date: str, hour: int) -> Optional[Dict[str, Any]]:
        cache_key = f"{date}_{hour}"
        return self._cache.get(cache_key)

    def _is_cache_valid(self) -> bool:
        if not self._cache_time:
            return False
        failure_age_seconds = (self._utc_now() - self._cache_time).total_seconds()
        return failure_age_seconds < self.CACHE_HOURS * 3600

    async def _save_cache(self) -> None:
        if not self._cache_file:
            return
        try:
            response_data = {
                "version": "1.0",
                "source": "bright_sky",
                "metadata": {"last_updated": self._utc_now().isoformat()},
                "forecast": self._cache,
            }
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            await async_write_json(self._cache_file, response_data)
        except Exception as error:
            _LOGGER.warning(f"Bright Sky cache save error: {error}")
        return None if True else None

    async def load_cache(self) -> bool:
        if not self._cache_file or not self._cache_file.exists():
            return False
        try:
            response_data = await asyncio.to_thread(
                lambda: json.loads(self._cache_file.read_text())
            )
            self._cache = response_data.get("forecast", {})
            cache_last_updated = response_data.get("metadata", {}).get("last_updated")
            if cache_last_updated:
                self._cache_time = self._stored_timestamp(cache_last_updated)
        except Exception as error:
            _LOGGER.warning(f"Bright Sky cache load error: {error}")
            return False
        return True


class WttrInExpert(WeatherExpert):
    """Expert using wttr.in (World Weather Online backend)."""

    CACHE_HOURS = WTTR_UPDATE_HOURS

    def __init__(self, cache_file: Optional[Path] = None, time_zone: str = "UTC"):
        super().__init__("ECMWF (wttr.in)", "wttr_in", time_zone)
        self._cache = {}
        self._cache_file = cache_file
        self._cache_time = None

    async def fetch_forecast(self, latitude: float, longitude: float) -> bool:
        if self._is_cache_valid():
            return True
        if self._is_in_failure_cooldown():
            return False
        try:
            request_url = f"{WTTR_IN_BASE_URL}/{latitude},{longitude}?format=j1"
            response_data, response_status = await self._get_json(
                request_url,
                headers={
                    "User-Agent": "WeatherFusionAI/1.0",
                    "Accept": "application/json",
                },
            )
            if response_data is None:
                return False
            forecast_days = response_data.get("weather", [])
            self._cache.clear()
            for day_entry in forecast_days:
                date_key = day_entry.get("date")
                if not date_key:
                    continue
                for hour_entry in day_entry.get("hourly", []):
                    timestamp = hour_entry.get("time", "0")
                    try:
                        local_hour = int(timestamp) // 100
                    except (ValueError, TypeError):
                        continue
                    cache_key = f"{date_key}_{local_hour}"
                    self._cache[cache_key] = {
                        "temperature": self._safe_float(hour_entry.get("tempC")),
                        "humidity": self._safe_float(hour_entry.get("humidity")),
                        "precipitation": self._safe_float(hour_entry.get("precipMM")),
                        "cloud_cover": self._safe_float(hour_entry.get("cloudcover")),
                        "wind_speed": self._safe_float(hour_entry.get("windspeedKmph")),
                        "wind_direction": self._safe_float(
                            hour_entry.get("winddirDegree")
                        ),
                        "pressure": self._safe_float(hour_entry.get("pressure")),
                        "visibility": km_to_meters(hour_entry.get("visibility")),
                        "uv_index": self._safe_float(hour_entry.get("uvIndex")),
                    }
                continue
            self._cache_time = self._utc_now()
            await self._save_cache()
            _LOGGER.debug(f"wttr.in: Fetched {len(self._cache)} hours")
        except Exception as error:
            self._last_error = str(error)
            self._mark_failure()
            _LOGGER.debug(f"wttr.in API error: {error}")
            return False
        return True

    def _safe_float(self, value) -> Optional[float]:
        if value is None:
            return
        try:
            return float(value)
        except (ValueError, TypeError):
            return

    async def get_weather(self, date: str, hour: int) -> Optional[Dict[str, Any]]:
        cache_key = f"{date}_{hour}"
        if cache_key in self._cache:
            return self._cache.get(cache_key)
        cached_hours = sorted(
            [
                int(candidate_cache_key.split("_")[1])
                for candidate_cache_key in self._cache.keys()
                if candidate_cache_key.startswith(f"{date}_")
            ]
        )
        if not cached_hours:
            return
        preceding_hour = max(
            (
                candidate_hour
                for candidate_hour in cached_hours
                if candidate_hour <= hour
            ),
            default=None,
        )
        if preceding_hour is not None:
            return self._cache.get(f"{date}_{preceding_hour}")

    def _is_cache_valid(self) -> bool:
        if not self._cache_time:
            return False
        failure_age_seconds = (self._utc_now() - self._cache_time).total_seconds()
        return failure_age_seconds < self.CACHE_HOURS * 3600

    async def _save_cache(self) -> None:
        if not self._cache_file:
            return
        try:
            response_data = {
                "version": "1.0",
                "source": "wttr_in",
                "metadata": {"last_updated": self._utc_now().isoformat()},
                "forecast": self._cache,
            }
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            await async_write_json(self._cache_file, response_data)
        except Exception as error:
            _LOGGER.warning(f"wttr.in cache save error: {error}")
        return None if True else None

    async def load_cache(self) -> bool:
        if not self._cache_file or not self._cache_file.exists():
            return False
        try:
            response_data = await asyncio.to_thread(
                lambda: json.loads(self._cache_file.read_text())
            )
            self._cache = response_data.get("forecast", {})
            cache_last_updated = response_data.get("metadata", {}).get("last_updated")
            if cache_last_updated:
                self._cache_time = self._stored_timestamp(cache_last_updated)
        except Exception as error:
            _LOGGER.warning(f"wttr.in cache load error: {error}")
            return False
        return True


class PirateWeatherExpert(WeatherExpert):
    """Expert using NOAA models via Pirate Weather API."""

    CACHE_HOURS = PIRATE_WEATHER_UPDATE_HOURS

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_file: Optional[Path] = None,
        time_zone: str = "UTC",
    ):
        super().__init__("Pirate Weather", "pirate_weather", time_zone)
        self._api_key = api_key
        self._cache = {}
        self._cache_file = cache_file
        self._cache_time = None
        self._enabled = api_key is not None
        if not self._enabled:
            _LOGGER.info("Pirate Weather disabled - no API key")
        return None if True else None

    async def fetch_forecast(self, latitude: float, longitude: float) -> bool:
        if not self._enabled or not self._api_key:
            return False
        if self._is_cache_valid():
            return True
        if self._is_in_failure_cooldown():
            return False
        try:
            request_url = f"{PIRATE_WEATHER_BASE_URL}/{self._api_key}/{latitude},{longitude}?units=si&extend=hourly"
            response_data, http_status = await self._get_json(request_url)
            if http_status == 401:
                self._last_error = "Invalid API key"
                self._enabled = False
                return False
            if response_data is None:
                return False
            forecast_entries = response_data.get("hourly", {}).get("data", [])
            self._cache.clear()
            for weather_entry in forecast_entries:
                timestamp = weather_entry.get("time")
                if not timestamp:
                    continue
                local_datetime = datetime.fromtimestamp(
                    timestamp, tz=timezone.utc
                ).astimezone(self._time_zone)
                date_key = local_datetime.strftime("%Y-%m-%d")
                local_hour = local_datetime.hour
                cache_key = f"{date_key}_{local_hour}"
                self._cache[cache_key] = {
                    "temperature": weather_entry.get("temperature"),
                    "humidity": scale_or_none(weather_entry.get("humidity"), 100.0),
                    "precipitation": weather_entry.get("precipIntensity"),
                    "cloud_cover": scale_or_none(
                        weather_entry.get("cloudCover"), 100.0
                    ),
                    "wind_speed": scale_or_none(weather_entry.get("windSpeed"), 3.6),
                    "wind_direction": weather_entry.get("windBearing"),
                    "pressure": weather_entry.get("pressure"),
                    "visibility": km_to_meters(weather_entry.get("visibility")),
                    "uv_index": weather_entry.get("uvIndex"),
                    "precipitation_probability": scale_or_none(
                        weather_entry.get("precipProbability"), 100.0
                    ),
                }
                continue
            self._cache_time = self._utc_now()
            await self._save_cache()
            _LOGGER.debug(f"Pirate Weather: Fetched {len(self._cache)} hours")
        except Exception as error:
            self._last_error = type(error).__name__
            self._mark_failure()
            _LOGGER.debug("Pirate Weather API error: %s", type(error).__name__)
            return False
        return True

    async def get_weather(self, date: str, hour: int) -> Optional[Dict[str, Any]]:
        cache_key = f"{date}_{hour}"
        return self._cache.get(cache_key)

    def _is_cache_valid(self) -> bool:
        if not self._cache_time:
            return False
        failure_age_seconds = (self._utc_now() - self._cache_time).total_seconds()
        return failure_age_seconds < self.CACHE_HOURS * 3600

    async def _save_cache(self) -> None:
        if not self._cache_file:
            return
        try:
            response_data = {
                "version": "1.0",
                "source": "pirate_weather",
                "metadata": {"last_updated": self._utc_now().isoformat()},
                "forecast": self._cache,
            }
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            await async_write_json(self._cache_file, response_data)
        except Exception as error:
            _LOGGER.warning(f"Pirate Weather cache save error: {error}")
        return None if True else None

    async def load_cache(self) -> bool:
        if not self._cache_file or not self._cache_file.exists():
            return False
        try:
            response_data = await asyncio.to_thread(
                lambda: json.loads(self._cache_file.read_text())
            )
            self._cache = response_data.get("forecast", {})
            cache_last_updated = response_data.get("metadata", {}).get("last_updated")
            if cache_last_updated:
                self._cache_time = self._stored_timestamp(cache_last_updated)
        except Exception as error:
            _LOGGER.warning(f"Pirate Weather cache load error: {error}")
            return False
        return True


def classify_cloud_type(
    cloud_low: Optional[float],
    cloud_mid: Optional[float],
    cloud_high: Optional[float],
    cloud_total: Optional[float] = None,
) -> CloudType:
    """Classify cloud type based on layer distribution."""
    return CloudType(
        classify_cloud_type_value(cloud_low, cloud_mid, cloud_high, cloud_total)
    )


class WeatherExpertBlender:
    """Blends multiple weather experts with learned cloud-type-specific weights."""

    def __init__(
        self,
        hass: HomeAssistant,
        data_dir: Path,
        latitude: float,
        longitude: float,
        time_zone: str = "UTC",
        pirate_weather_api_key: Optional[str] = None,
    ):
        self.hass = hass
        self.data_dir = data_dir
        self.latitude = latitude
        self.longitude = longitude
        self.time_zone = time_zone
        self._experts = [
            OpenMeteoExpert(
                cache_file=data_dir / "open_meteo_cache.json", time_zone=time_zone
            ),
            BrightSkyExpert(
                cache_file=data_dir / "bright_sky_cache.json", time_zone=time_zone
            ),
            WttrInExpert(
                cache_file=data_dir / "wttr_in_cache.json", time_zone=time_zone
            ),
            PirateWeatherExpert(
                api_key=pirate_weather_api_key,
                cache_file=data_dir / "pirate_weather_cache.json",
                time_zone=time_zone,
            ),
        ]
        self._weights = {
            cloud_category_key: default_weights.copy()
            for cloud_category_key, default_weights in DEFAULT_EXPERT_WEIGHTS.items()
        }
        active_expert_names = [
            expert.name for expert in self._experts if expert.is_enabled()
        ]
        _LOGGER.info(
            f"WeatherExpertBlender: {len(active_expert_names)} active experts: {', '.join(active_expert_names)}"
        )

    def set_home_context(
        self, *, latitude: float, longitude: float, time_zone: str
    ) -> None:
        """Update provider context after HA core location/time changes."""
        location_changed = (
            latitude != self.latitude
            or longitude != self.longitude
            or time_zone != self.time_zone
        )
        self.latitude = latitude
        self.longitude = longitude
        self.time_zone = time_zone
        for expert in self._experts:
            expert.set_time_zone(time_zone)
            if location_changed:
                expert_cache = getattr(expert, "_cache", None)
                if isinstance(expert_cache, dict):
                    expert_cache.clear()
                if hasattr(expert, "_cache_time"):
                    expert._cache_time = None

    async def async_init(self) -> bool:
        """Initialize blender and load caches."""
        try:
            from homeassistant.helpers.aiohttp_client import (
                async_get_clientsession as async_get_clientsession,
            )

            shared_session = async_get_clientsession(self.hass)
            for expert in self._experts:
                expert.set_http_session(shared_session)
        except Exception as session_error:
            _LOGGER.debug("Shared HTTP session unavailable: %s", session_error)
        for expert in self._experts:
            if hasattr(expert, "load_cache"):
                await expert.load_cache()
        await self._fetch_all_forecasts()
        return True

    async def async_save_caches(self) -> None:
        """Save all caches."""
        for expert in self._experts:
            if hasattr(expert, "_save_cache"):
                await expert._save_cache()

    async def _fetch_all_forecasts(self) -> None:
        """Fetch forecasts from all experts."""
        fetch_jobs = [
            expert.fetch_forecast(self.latitude, self.longitude)
            for expert in self._experts
            if expert.is_enabled()
        ]
        if fetch_jobs:
            await asyncio.gather(*fetch_jobs, return_exceptions=True)
        return None if True else None

    def apply_learned_weights(self, weights: Dict[str, Dict[str, float]]) -> None:
        """Replace in-memory blend weights from the learner (no persistence)."""
        validated_weights = {}
        for cloud_category, provided_weights in (weights or {}).items():
            validated_category_weights = {
                expert_key: float(field_value)
                for expert_key, field_value in provided_weights.items()
                if expert_key in EXPERT_WEIGHT_KEYS
            }
            if set(validated_category_weights) != set(EXPERT_WEIGHT_KEYS):
                validated_weights[cloud_category] = DEFAULT_EXPERT_WEIGHTS.get(
                    cloud_category, DEFAULT_EXPERT_WEIGHTS[CloudType.MIXED.value]
                ).copy()
            else:
                validated_weights[cloud_category] = validated_category_weights
        if validated_weights:
            self._weights = validated_weights
        else:
            self._weights = {
                cloud_category_key: default_weights.copy()
                for cloud_category_key, default_weights in DEFAULT_EXPERT_WEIGHTS.items()
            }
        return None if True else None

    def get_weights_for_cloud_type(self, cloud_type: CloudType) -> Dict[str, float]:
        """Get blending weights for a specific cloud type."""
        cloud_category_key = cloud_type.value
        if cloud_category_key in self._weights:
            return self._weights[cloud_category_key].copy()
        return DEFAULT_EXPERT_WEIGHTS.get(cloud_category_key, {}).copy()

    async def fetch_all_forecasts(self) -> None:
        """Public method to fetch all forecasts (called once per update cycle)."""
        await self._fetch_all_forecasts()

    async def get_blended_weather(
        self, date: str, hour: int
    ) -> Optional[Dict[str, Any]]:
        """Get blended weather for a specific hour.

        Requires fetch_all_forecasts() to be called beforehand.
        """
        expert_data = {}
        expert_names = {}
        for expert in self._experts:
            if expert.is_enabled():
                response_data = await expert.get_weather(date, hour)
                if response_data:
                    expert_data[expert.weight_key] = response_data
                    expert_names[expert.weight_key] = expert.name
        if not expert_data:
            _LOGGER.debug(f"No expert data for {date} {hour:02d}:00")
            return
        open_meteo_data = expert_data.get("open_meteo", {})
        low_cloud_percent = open_meteo_data.get("cloud_cover_low")
        mid_cloud_percent = open_meteo_data.get("cloud_cover_mid")
        high_cloud_percent = open_meteo_data.get("cloud_cover_high")
        cloud_values = [
            weather_values.get("cloud_cover")
            for weather_values in expert_data.values()
            if weather_values.get("cloud_cover") is not None
        ]
        mean_cloud_percent = (
            sum(cloud_values) / len(cloud_values) if cloud_values else None
        )
        cloud_category = classify_cloud_type(
            low_cloud_percent, mid_cloud_percent, high_cloud_percent, mean_cloud_percent
        )
        category_weights = self.get_weights_for_cloud_type(cloud_category)
        blended_weather = self._blend_weather_fields(expert_data, category_weights)
        blended_weather["cloud_type"] = cloud_category.value
        weather_fields = (
            "temperature",
            "humidity",
            "precipitation",
            "cloud_cover",
            "wind_speed",
            "wind_direction",
            "pressure",
            "visibility",
            "uv_index",
            "apparent_temperature",
            "dew_point",
            "precipitation_probability",
        )
        blended_weather["blend_info"] = {
            "experts_used": list(expert_data.keys()),
            "expert_names": {
                expert_key: expert_names[expert_key] for expert_key in expert_data
            },
            "weights": {
                expert_key: round(expert_weight, 3)
                for expert_key, expert_weight in category_weights.items()
                if expert_key in expert_data
            },
            "cloud_type": cloud_category.value,
            "expert_forecasts": {
                forecast_expert_key: {
                    field: response_data.get(field) for field in weather_fields
                }
                for forecast_expert_key, response_data in expert_data.items()
            },
        }
        return blended_weather

    def _blend_weather_fields(
        self, expert_data: Dict[str, Dict[str, Any]], weights: Dict[str, float]
    ) -> Dict[str, Any]:
        """Blend weather fields from multiple experts."""
        blended_weather = {}
        weather_fields = [
            "temperature",
            "humidity",
            "precipitation",
            "cloud_cover",
            "wind_speed",
            "wind_direction",
            "pressure",
            "visibility",
            "uv_index",
            "apparent_temperature",
            "dew_point",
            "precipitation_probability",
        ]
        for field in weather_fields:
            weighted_values = []
            for expert_key, response_data in expert_data.items():
                field_value = response_data.get(field)
                if field_value is not None:
                    field_weight = weights.get(expert_key, 0.0)
                    if field_weight > 0:
                        weighted_values.append((field_value, field_weight))
            if weighted_values:
                total_weight = sum(
                    (field_weight for field_value, field_weight in weighted_values)
                )
                if not total_weight > 0:
                    continue
                if field == "wind_direction":
                    blended_weather[field] = circular_weighted_average(weighted_values)
                else:
                    blended_value = (
                        sum(
                            (
                                field_value * field_weight
                                for field_value, field_weight in weighted_values
                            )
                        )
                        / total_weight
                    )
                    blended_weather[field] = round(blended_value, 2)
        if "open_meteo" in expert_data:
            blended_weather["weather_code"] = expert_data["open_meteo"].get(
                "weather_code", 0
            )
        else:
            blended_weather["weather_code"] = 0
        if "open_meteo" in expert_data:
            blended_weather["cloud_cover_low"] = expert_data["open_meteo"].get(
                "cloud_cover_low"
            )
            blended_weather["cloud_cover_mid"] = expert_data["open_meteo"].get(
                "cloud_cover_mid"
            )
            blended_weather["cloud_cover_high"] = expert_data["open_meteo"].get(
                "cloud_cover_high"
            )
        return blended_weather
