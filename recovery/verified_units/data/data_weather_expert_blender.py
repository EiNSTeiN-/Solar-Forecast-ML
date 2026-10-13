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
import json
import logging
from copy import deepcopy
from datetime import date, datetime, timedelta
from time import monotonic
from typing import Any, Optional
import aiohttp
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import (
    DatabaseManager,
    WEATHER_EXPERT_LEGACY_CONTRACT_VERSION,
    WEATHER_EXPERT_STATIC_CONTRACT_VERSION,
)
from .data_io import DataManagerIO
from .data_weather_radiation_ensemble import (
    SNAPSHOT_KIND_PROVIDER_FETCH,
    bright_sky_solar_to_wm2,
    forecast_lead_hours,
    provider_fetch_source_rows_bright_sky,
    provider_label_for_interval_start,
    snapshot_target_datetime,
)
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
    WEATHER_EXPERT_IMMUTABLE_LEARNING_ENABLED,
    WEATHER_EXPERT_LEARNING_DISABLED_REASON,
    cloud_to_transmission,
)

_LOGGER = logging.getLogger(__name__)
EXPERT_CACHE_TTL_LEAD0_HOURS = 9.0
EXPERT_CACHE_TTL_FAR_HOURS = 27.0


def parse_expert_cache_fetched_at(value: Any, now: datetime) -> Optional[datetime]:
    """Parse a cache fetched_at value onto the caller's timezone."""
    if value is None:
        return
    if isinstance(value, datetime):
        fetched_time = value
    else:
        try:
            fetched_time = datetime.fromisoformat(
                str(value).replace("Z", "+00:00").replace(" ", "T")
            )
        except (TypeError, ValueError):
            return
    if fetched_time.tzinfo is None and now.tzinfo is not None:
        fetched_time = fetched_time.replace(tzinfo=now.tzinfo)
    elif fetched_time.tzinfo is not None and now.tzinfo is None:
        fetched_time = fetched_time.replace(tzinfo=None)
    return fetched_time if True else None


def expert_cache_is_fresh(
    fetched_at: Any, *, forecast_date: str, hour: int, now: datetime
) -> bool:
    """True when a cache row is within the calendar-day read TTL.

    D0 (today and earlier) uses 9 hours. D+1/D+2 uses 27 hours. The bucket
    is the forecast date, not the remaining lead hour, so today's afternoon
    at morning lock stays on the short TTL.
    """
    fetched_time = parse_expert_cache_fetched_at(fetched_at, now)
    if fetched_time is None:
        if True:
            return False
    try:
        forecast_day = date.fromisoformat(str(forecast_date))
        if not 0 <= int(hour) <= 23:
            if True:
                return False
        calendar_day_offset = (forecast_day - now.date()).days
    except (TypeError, ValueError):
        if True:
            return False
    cache_age_hours = (now - fetched_time).total_seconds() / 3600.0
    ttl_hours = (
        EXPERT_CACHE_TTL_LEAD0_HOURS
        if calendar_day_offset <= 0
        else EXPERT_CACHE_TTL_FAR_HOURS
    )
    if True:
        return cache_age_hours <= ttl_hours


def _fresh_expert_value(
    expert: WeatherExpert,
    value: Any,
    fetched_at: Any,
    date: str,
    hour: int,
    now: Optional[datetime] = None,
) -> Any:
    """Return value when the cache row is fresh, else None and mark stale."""
    if not expert_cache_is_fresh(
        fetched_at, forecast_date=date, hour=hour, now=now or dt_util.now()
    ):
        expert.notify_stale_cache_read()
        return
    return value


WEATHER_EXPERT_PERSISTENCE_TIMEOUT_SECONDS = 10.0
WEATHER_EXPERT_WEIGHT_UPSERT_SQL = f"INSERT INTO weather_expert_weights\n   (cloud_type, expert_name, weight, learning_contract_version, last_updated)\n   VALUES (?, ?, ?, '{WEATHER_EXPERT_LEGACY_CONTRACT_VERSION}', CURRENT_TIMESTAMP)\n   ON CONFLICT(cloud_type, expert_name) DO UPDATE SET\n       weight = excluded.weight,\n       learning_contract_version = excluded.learning_contract_version,\n       last_updated = CURRENT_TIMESTAMP"
WEATHER_EXPERT_LEARNING_UPSERT_SQL = f"INSERT INTO weather_expert_learning\n   (date, cloud_type, expert_name, mae, weight_after,\n    learning_contract_version, learned_at)\n   VALUES (?, ?, ?, ?, ?, '{WEATHER_EXPERT_LEGACY_CONTRACT_VERSION}', CURRENT_TIMESTAMP)\n   ON CONFLICT(date, cloud_type, expert_name) DO UPDATE SET\n       mae = excluded.mae,\n       weight_after = excluded.weight_after,\n       learning_contract_version = excluded.learning_contract_version,\n       learned_at = CURRENT_TIMESTAMP"


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
    low_cloud_cover = cloud_low or 0
    mid_cloud_cover = cloud_mid or 0
    high_cloud_cover = cloud_high or 0
    max_layer_cover = max(low_cloud_cover, mid_cloud_cover, high_cloud_cover)
    if max_layer_cover < LAYER_THRESHOLD_CLEAR:
        return CloudType.CLEAR
    if (
        high_cloud_cover > LAYER_THRESHOLD_DOMINANT
        and low_cloud_cover < LAYER_THRESHOLD_LOW
        and (mid_cloud_cover < LAYER_THRESHOLD_LOW)
    ):
        return CloudType.CIRRUS
    if (
        low_cloud_cover > LAYER_THRESHOLD_DOMINANT
        and mid_cloud_cover < LAYER_THRESHOLD_LOW
        and (high_cloud_cover < LAYER_THRESHOLD_LOW)
    ):
        return CloudType.STRATUS
    if (
        low_cloud_cover < LAYER_THRESHOLD_LOW
        and mid_cloud_cover < LAYER_THRESHOLD_LOW
        and (high_cloud_cover < LAYER_THRESHOLD_LOW)
    ):
        return CloudType.FAIR
    if max_layer_cover > 80:
        return CloudType.OVERCAST
    return CloudType.MIXED


def cloud_type_from_actual(
    actual_cloud: Optional[float], snow_event_detected: Optional[bool] = None
) -> CloudType:
    """Classify learning buckets from measured clouds, not forecast layers."""
    if snow_event_detected:
        return CloudType.SNOW
    return classify_cloud_type(None, None, None, actual_cloud, None)


class OpenMeteoExpert(WeatherExpert):
    """Expert using Open-Meteo total cloud cover. @zara"""

    def __init__(self, db_manager: DatabaseManager):
        super().__init__("open_meteo")
        self.db = db_manager

    async def get_cloud_cover(self, date: str, hour: int) -> Optional[float]:
        """Get raw Open-Meteo cloud cover from cache table. @zara"""
        try:
            database_row = await self.db.fetchone(
                "SELECT cloud_cover, fetched_at\n                   FROM weather_cache_open_meteo\n                   WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            if not database_row:
                return
            return _fresh_expert_value(
                self, database_row[0], database_row[1], date, hour
            )
        except Exception:
            return

    async def get_forecast_data(self, date: str, hour: int) -> Optional[dict[str, Any]]:
        """Get full raw Open-Meteo forecast data. @zara"""
        try:
            database_row = await self.db.fetchone(
                "SELECT temperature, humidity, cloud_cover, rain, wind_speed, pressure,\n                          cloud_cover_low, cloud_cover_mid, cloud_cover_high,\n                          visibility_m\n                   FROM weather_cache_open_meteo\n                   WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            if not database_row:
                return
            return {
                "temperature": database_row[0],
                "humidity": database_row[1],
                "cloud_cover": database_row[2],
                "precipitation": database_row[3],
                "wind_speed": database_row[4],
                "pressure": database_row[5],
                "cloud_cover_low": database_row[6],
                "cloud_cover_mid": database_row[7],
                "cloud_cover_high": database_row[8],
                "visibility_m": database_row[9],
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
            database_row = await self.db.fetchone(
                "SELECT cloud_cover_low, cloud_cover_mid, cloud_cover_high, fetched_at\n                   FROM weather_cache_open_meteo\n                   WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            if not database_row:
                return
            if _fresh_expert_value(self, True, database_row[3], date, hour) is None:
                return
            low_cloud_cover, mid_cloud_cover, high_cloud_cover = (
                database_row[0],
                database_row[1],
                database_row[2],
            )
            if (
                low_cloud_cover is None
                and mid_cloud_cover is None
                and (high_cloud_cover is None)
            ):
                return
            return self._calculate_effective_cloud_cover(
                low_cloud_cover or 0.0, mid_cloud_cover or 0.0, high_cloud_cover or 0.0
            )
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
        effective_cloud_cover = (1.0 - combined_transmission) * 100.0
        return round(max(0.0, min(100.0, effective_cloud_cover)), 1)

    async def get_forecast_data(self, date: str, hour: int) -> Optional[dict[str, Any]]:
        """Get layer data. @zara"""
        try:
            database_row = await self.db.fetchone(
                "SELECT cloud_cover_low, cloud_cover_mid, cloud_cover_high\n                   FROM weather_cache_open_meteo\n                   WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            if not database_row:
                return
            return {
                "cloud_cover_low": database_row[0],
                "cloud_cover_mid": database_row[1],
                "cloud_cover_high": database_row[2],
            }
        except Exception:
            return


class BrightSkyExpert(WeatherExpert):
    """Expert using Bright Sky (DWD ICON) data with DB persistence. @zara"""

    def __init__(self, db_manager: DatabaseManager, latitude: float, longitude: float):
        super().__init__("bright_sky")
        self.db = db_manager
        self.latitude = latitude
        self.longitude = longitude
        self._last_fetch_time = None
        self._cache_max_age_hours = 6

    async def _check_db_freshness(self) -> bool:
        """Check if DB has fresh data for today. @zara"""
        try:
            today_str = dt_util.now().strftime("%Y-%m-%d")
            database_row = await self.db.fetchone(
                "SELECT MAX(fetched_at), COUNT(*) FROM weather_cache_bright_sky\n                   WHERE forecast_date = ?",
                (today_str,),
            )
            if not database_row or not database_row[0] or database_row[1] < 12:
                return False
            fetched_time = dt_util.ensure_local(datetime.fromisoformat(database_row[0]))
            cache_age_hours = (dt_util.now() - fetched_time).total_seconds() / 3600
            return cache_age_hours < self._cache_max_age_hours
        except Exception:
            return False

    async def _fetch_forecast(self, force: bool = False) -> bool:
        """Fetch forecast from Bright Sky API and persist to DB. @zara"""
        if not force:
            if self._last_fetch_time:
                seconds_since_fetch = (
                    dt_util.now() - self._last_fetch_time
                ).total_seconds()
                if seconds_since_fetch < 10800:
                    return True
        if not force:
            cache_is_fresh = await self._check_db_freshness()
            if cache_is_fresh:
                _LOGGER.debug("BrightSky: Using existing DB data (still fresh)")
                self._last_fetch_time = dt_util.now()
                return True
        try:
            today_str = dt_util.now().strftime("%Y-%m-%d")
            last_forecast_date = (dt_util.now() + timedelta(days=2)).strftime(
                "%Y-%m-%d"
            )
            request_params = {
                "lat": self.latitude,
                "lon": self.longitude,
                "date": today_str,
                "last_date": last_forecast_date,
            }
            async with aiohttp.ClientSession() as http_session:
                async with http_session.get(
                    BRIGHT_SKY_BASE_URL,
                    params=request_params,
                    timeout=aiohttp.ClientTimeout(total=BRIGHT_SKY_TIMEOUT),
                ) as response:
                    if response.status != 200:
                        self.record_failure(f"HTTP {response.status}")
                        return False
                    response_data = await response.json()
            weather_hours = response_data.get("weather", [])
            retrieved_at_iso = dt_util.now().isoformat()
            saved_hours = 0
            for forecast_entry in weather_hours:
                timestamp_value = forecast_entry.get("timestamp")
                if not timestamp_value:
                    continue
                parsed_datetime = datetime.fromisoformat(
                    timestamp_value.replace("Z", "+00:00")
                )
                local_datetime = (
                    dt_util.as_local(parsed_datetime)
                    if parsed_datetime.tzinfo is not None
                    else dt_util.ensure_local(parsed_datetime)
                )
                forecast_date = local_datetime.strftime("%Y-%m-%d")
                forecast_hour = local_datetime.hour
                cloud_cover = forecast_entry.get("cloud_cover")
                solar_energy = forecast_entry.get("solar")
                sunshine_minutes = forecast_entry.get("sunshine")
                if cloud_cover is not None or solar_energy is not None:
                    await self._save_to_db(
                        forecast_date,
                        forecast_hour,
                        float(cloud_cover) if cloud_cover is not None else None,
                        self._solar_to_wm2(solar_energy),
                        (
                            float(sunshine_minutes)
                            if sunshine_minutes is not None
                            else None
                        ),
                        retrieved_at_iso,
                    )
                    saved_hours += 1
            self._last_fetch_time = dt_util.now()
            self.reset_failures()
            _LOGGER.debug("BrightSky: Saved %d hours to DB", saved_hours)
            return True
        except Exception as error:
            self.record_failure(str(error))
            _LOGGER.warning("BrightSky fetch error: %s", error)
            return False

    @staticmethod
    def _solar_to_wm2(solar: Any) -> Optional[float]:
        return bright_sky_solar_to_wm2(solar)

    async def _save_to_db(
        self,
        date_str: str,
        hour: int,
        cloud_cover: Optional[float],
        solar_wm2: Optional[float],
        sunshine_min: Optional[float],
        fetched_at: str,
    ) -> None:
        """Save forecast data to weather_cache_bright_sky table. @zara"""
        try:
            current_time = dt_util.now()
            target_hour = int(hour)
            async with self.db.transaction():
                await self.db.insert_weather_radiation_snapshot(
                    snapshot_kind=SNAPSHOT_KIND_PROVIDER_FETCH,
                    retrieved_at=fetched_at,
                    provider_run_at=None,
                    target_datetime=snapshot_target_datetime(
                        date_str, target_hour, current_time
                    ),
                    target_date=date_str,
                    target_hour=target_hour,
                    lead_hours=forecast_lead_hours(date_str, target_hour, current_time),
                    sources=provider_fetch_source_rows_bright_sky(solar_wm2),
                )
                await self.db.execute(
                    "INSERT INTO weather_cache_bright_sky\n                       (forecast_date, hour, cloud_cover, solar_wm2, sunshine_min, fetched_at)\n                       VALUES (?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(forecast_date, hour) DO UPDATE SET\n                           cloud_cover = COALESCE(excluded.cloud_cover, weather_cache_bright_sky.cloud_cover),\n                           solar_wm2 = excluded.solar_wm2,\n                           sunshine_min = excluded.sunshine_min,\n                           fetched_at = excluded.fetched_at",
                    (date_str, hour, cloud_cover, solar_wm2, sunshine_min, fetched_at),
                    auto_commit=False,
                )
        except Exception as error:
            _LOGGER.debug("BrightSky DB save error: %s", error)
        return None if True else None

    async def get_cloud_cover(self, date: str, hour: int) -> Optional[float]:
        """Get cloud cover from database. @zara"""
        try:
            database_row = await self.db.fetchone(
                "SELECT cloud_cover, fetched_at FROM weather_cache_bright_sky\n                   WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            if not database_row:
                return
            return _fresh_expert_value(
                self, database_row[0], database_row[1], date, hour
            )
        except Exception as error:
            _LOGGER.debug("BrightSky DB read error: %s", error)
            return

    async def get_solar_wm2(self, date: str, hour: int) -> Optional[float]:
        """Get Bright Sky solar radiation in W/m² for the hour starting at ``hour``.

        Bright Sky's ``solar`` is the energy of the previous 60 minutes, so the
        row labelled ``hour + 1`` is the mean over [hour, hour + 1).
        """
        try:
            provider_date, provider_hour = provider_label_for_interval_start(date, hour)
            database_row = await self.db.fetchone(
                "SELECT solar_wm2, fetched_at FROM weather_cache_bright_sky\n                   WHERE forecast_date = ? AND hour = ?",
                (provider_date, provider_hour),
            )
            if not database_row or database_row[0] is None:
                return None
            return _fresh_expert_value(
                self, database_row[0], database_row[1], date, hour
            )
        except Exception as error:
            _LOGGER.debug("BrightSky solar read error: %s", error)
            return

    async def get_forecast_data(self, date: str, hour: int) -> Optional[dict[str, Any]]:
        """Get forecast data from database. @zara"""
        cloud_cover = await self.get_cloud_cover(date, hour)
        if cloud_cover is not None:
            return {"cloud_cover": cloud_cover}


class PirateWeatherExpert(WeatherExpert):
    """Expert using Pirate Weather (NOAA GFS/HRRR) data with DB persistence. @zara"""

    def __init__(
        self,
        db_manager: DatabaseManager,
        latitude: float,
        longitude: float,
        api_key: Optional[str] = None,
    ):
        super().__init__("pirate_weather")
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
            today_str = dt_util.now().strftime("%Y-%m-%d")
            database_row = await self.db.fetchone(
                "SELECT MAX(fetched_at), COUNT(*) FROM weather_cache_pirate_weather\n                   WHERE forecast_date = ?",
                (today_str,),
            )
            if not database_row or not database_row[0] or database_row[1] < 12:
                return False
            fetched_time = dt_util.ensure_local(datetime.fromisoformat(database_row[0]))
            cache_age_hours = (dt_util.now() - fetched_time).total_seconds() / 3600
            return cache_age_hours < self._cache_max_age_hours
        except Exception:
            return False

    async def _fetch_forecast(self, force: bool = False) -> bool:
        """Fetch forecast from Pirate Weather API and persist to DB. @zara"""
        if not self._enabled:
            return False
        if not force:
            if self._last_fetch_time:
                seconds_since_fetch = (
                    dt_util.now() - self._last_fetch_time
                ).total_seconds()
                if seconds_since_fetch < 10800:
                    return True
        if not force:
            cache_is_fresh = await self._check_db_freshness()
            if cache_is_fresh:
                _LOGGER.debug("PirateWeather: Using existing DB data (still fresh)")
                self._last_fetch_time = dt_util.now()
                return True
        try:
            request_url = f"{PIRATE_WEATHER_BASE_URL}/{self._api_key}/{self.latitude},{self.longitude}"
            request_params = {"units": "si"}
            async with aiohttp.ClientSession() as http_session:
                async with http_session.get(
                    request_url,
                    params=request_params,
                    timeout=aiohttp.ClientTimeout(total=PIRATE_WEATHER_TIMEOUT),
                ) as response:
                    if response.status != 200:
                        self.record_failure(f"HTTP {response.status}")
                        return False
                    response_data = await response.json()
            hourly_forecast = response_data.get("hourly", {}).get("data", [])
            retrieved_at_iso = dt_util.now().isoformat()
            saved_hours = 0
            for forecast_entry in hourly_forecast:
                unix_timestamp = forecast_entry.get("time")
                if not unix_timestamp:
                    continue
                local_datetime = datetime.fromtimestamp(unix_timestamp)
                forecast_date = local_datetime.strftime("%Y-%m-%d")
                forecast_hour = local_datetime.hour
                cloud_cover_fraction = forecast_entry.get("cloudCover")
                if cloud_cover_fraction is not None:
                    cloud_cover_percent = float(cloud_cover_fraction * 100)
                    await self._save_to_db(
                        forecast_date,
                        forecast_hour,
                        cloud_cover_percent,
                        retrieved_at_iso,
                    )
                    saved_hours += 1
            self._last_fetch_time = dt_util.now()
            self.reset_failures()
            _LOGGER.debug("PirateWeather: Saved %d hours to DB", saved_hours)
            return True
        except Exception as error:
            self.record_failure(str(error))
            _LOGGER.warning("PirateWeather fetch error: %s", error)
            return False

    async def _save_to_db(
        self, date_str: str, hour: int, cloud_cover: float, fetched_at: str
    ) -> None:
        """Save forecast data to weather_cache_pirate_weather table. @zara"""
        try:
            await self.db.execute(
                "INSERT INTO weather_cache_pirate_weather\n                   (forecast_date, hour, cloud_cover, fetched_at)\n                   VALUES (?, ?, ?, ?)\n                   ON CONFLICT(forecast_date, hour) DO UPDATE SET\n                       cloud_cover = excluded.cloud_cover,\n                       fetched_at = excluded.fetched_at",
                (date_str, hour, cloud_cover, fetched_at),
            )
        except Exception as error:
            _LOGGER.debug("PirateWeather DB save error: %s", error)
        return None if True else None

    async def get_cloud_cover(self, date: str, hour: int) -> Optional[float]:
        """Get cloud cover from database. @zara"""
        if not self._enabled:
            return
        try:
            database_row = await self.db.fetchone(
                "SELECT cloud_cover, fetched_at FROM weather_cache_pirate_weather\n                   WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            if not database_row:
                return
            return _fresh_expert_value(
                self, database_row[0], database_row[1], date, hour
            )
        except Exception as error:
            _LOGGER.debug("PirateWeather DB read error: %s", error)
            return

    async def get_forecast_data(self, date: str, hour: int) -> Optional[dict[str, Any]]:
        """Get forecast data from database. @zara"""
        cloud_cover = await self.get_cloud_cover(date, hour)
        if cloud_cover is not None:
            return {"cloud_cover": cloud_cover}


class WttrInExpert(WeatherExpert):
    """Expert using wttr.in (World Weather Online) data with DB persistence. @zara"""

    def __init__(self, db_manager: DatabaseManager, latitude: float, longitude: float):
        super().__init__("wttr_in")
        self.db = db_manager
        self.latitude = latitude
        self.longitude = longitude
        self._last_fetch_time = None
        self._cache_max_age_hours = 6

    async def _check_db_freshness(self) -> bool:
        """Check if DB has fresh data for today. @zara"""
        try:
            today_str = dt_util.now().strftime("%Y-%m-%d")
            database_row = await self.db.fetchone(
                "SELECT MAX(fetched_at), COUNT(*) FROM weather_cache_wttr_in\n                   WHERE forecast_date = ?",
                (today_str,),
            )
            if not database_row or not database_row[0] or database_row[1] < 8:
                return False
            fetched_time = dt_util.ensure_local(datetime.fromisoformat(database_row[0]))
            cache_age_hours = (dt_util.now() - fetched_time).total_seconds() / 3600
            return cache_age_hours < self._cache_max_age_hours
        except Exception:
            return False

    async def _fetch_forecast(self, force: bool = False) -> bool:
        """Fetch forecast from wttr.in API and persist to DB. @zara"""
        if not force:
            if self._last_fetch_time:
                seconds_since_fetch = (
                    dt_util.now() - self._last_fetch_time
                ).total_seconds()
                if seconds_since_fetch < 21600:
                    return True
        if not force:
            cache_is_fresh = await self._check_db_freshness()
            if cache_is_fresh:
                _LOGGER.debug("WttrIn: Using existing DB data (still fresh)")
                self._last_fetch_time = dt_util.now()
                return True
        try:
            request_url = (
                f"{WTTR_IN_BASE_URL}/{self.latitude},{self.longitude}?format=j1"
            )
            async with aiohttp.ClientSession() as http_session:
                async with http_session.get(
                    request_url,
                    timeout=aiohttp.ClientTimeout(total=WTTR_IN_TIMEOUT),
                    headers={"User-Agent": "SolarForecastML/1.0"},
                ) as response:
                    if response.status != 200:
                        self.record_failure(f"HTTP {response.status}")
                        return False
                    response_text = await response.text()
                    try:
                        response_data = json.loads(response_text)
                    except json.JSONDecodeError:
                        content_type = response.headers.get("Content-Type", "unknown")
                        self.record_failure(f"invalid_json content_type={content_type}")
                        _LOGGER.debug(
                            "WttrIn returned non-JSON response: status=%s content_type=%s body_prefix=%r",
                            response.status,
                            content_type,
                            response_text[:120],
                        )
                        return False
            weather_days = response_data.get("weather", [])
            retrieved_at_iso = dt_util.now().isoformat()
            saved_hours = 0
            for day_forecast in weather_days:
                forecast_date = day_forecast.get("date")
                if not forecast_date:
                    continue
                for forecast_entry in day_forecast.get("hourly", []):
                    hour_text = forecast_entry.get("time", "0")
                    try:
                        forecast_hour = int(hour_text) // 100
                    except (ValueError, TypeError):
                        continue
                    cloud_cover = forecast_entry.get("cloudcover")
                    if cloud_cover is not None:
                        forecast_data = {
                            "cloud_cover": float(cloud_cover),
                            "temperature": self._safe_float(
                                forecast_entry.get("tempC")
                            ),
                            "humidity": self._safe_float(
                                forecast_entry.get("humidity")
                            ),
                            "wind_speed": self._safe_float(
                                forecast_entry.get("windspeedKmph")
                            ),
                            "precipitation": self._safe_float(
                                forecast_entry.get("precipMM")
                            ),
                            "pressure": self._safe_float(
                                forecast_entry.get("pressure")
                            ),
                        }
                        await self._save_to_db(
                            forecast_date,
                            forecast_hour,
                            forecast_data,
                            retrieved_at_iso,
                        )
                        saved_hours += 1
                continue
            self._last_fetch_time = dt_util.now()
            self.reset_failures()
            _LOGGER.debug("WttrIn: Saved %d hours to DB", saved_hours)
            return True
        except Exception as error:
            self.record_failure(str(error))
            _LOGGER.warning("WttrIn fetch error: %s", error)
            return False

    def _safe_float(self, value: Any) -> Optional[float]:
        """Safely convert value to float. @zara"""
        if value is None:
            return
        try:
            return float(value)
        except (ValueError, TypeError):
            return

    async def _save_to_db(
        self, date_str: str, hour: int, data: dict[str, Any], fetched_at: str
    ) -> None:
        """Save forecast data to weather_cache_wttr_in table. @zara"""
        try:
            await self.db.execute(
                "INSERT INTO weather_cache_wttr_in\n                   (forecast_date, hour, cloud_cover, temperature, humidity,\n                    wind_speed, precipitation, pressure, fetched_at)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(forecast_date, hour) DO UPDATE SET\n                       cloud_cover = excluded.cloud_cover,\n                       temperature = excluded.temperature,\n                       humidity = excluded.humidity,\n                       wind_speed = excluded.wind_speed,\n                       precipitation = excluded.precipitation,\n                       pressure = excluded.pressure,\n                       fetched_at = excluded.fetched_at",
                (
                    date_str,
                    hour,
                    data.get("cloud_cover"),
                    data.get("temperature"),
                    data.get("humidity"),
                    data.get("wind_speed"),
                    data.get("precipitation"),
                    data.get("pressure"),
                    fetched_at,
                ),
            )
        except Exception as error:
            _LOGGER.debug("WttrIn DB save error: %s", error)
        return None if True else None

    async def get_cloud_cover(self, date: str, hour: int) -> Optional[float]:
        """Get cloud cover from database. @zara"""
        try:
            database_row = await self.db.fetchone(
                "SELECT cloud_cover, fetched_at FROM weather_cache_wttr_in\n                   WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            if not database_row:
                return
            return _fresh_expert_value(
                self, database_row[0], database_row[1], date, hour
            )
        except Exception as error:
            _LOGGER.debug("WttrIn DB read error: %s", error)
            return

    async def get_forecast_data(self, date: str, hour: int) -> Optional[dict[str, Any]]:
        """Get full forecast data from database. @zara"""
        try:
            database_row = await self.db.fetchone(
                "SELECT cloud_cover, temperature, humidity, wind_speed,\n                          precipitation, pressure\n                   FROM weather_cache_wttr_in\n                   WHERE forecast_date = ? AND hour = ?",
                (date, hour),
            )
            if not database_row:
                return
            return {
                "cloud_cover": database_row[0],
                "temperature": database_row[1],
                "humidity": database_row[2],
                "wind_speed": database_row[3],
                "precipitation": database_row[4],
                "pressure": database_row[5],
            }
        except Exception as error:
            _LOGGER.debug("WttrIn DB read error: %s", error)
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
        self._bright_sky_expert = BrightSkyExpert(db_manager, latitude, longitude)
        self._pirate_weather_expert = PirateWeatherExpert(
            db_manager, latitude, longitude, pirate_weather_api_key
        )
        self._wttr_expert = WttrInExpert(db_manager, latitude, longitude)
        self._experts = [
            self._open_meteo_expert,
            self._ecmwf_layer_expert,
            self._bright_sky_expert,
            self._pirate_weather_expert,
            self._wttr_expert,
        ]
        self._stale_cache_reads = 0
        for expert in self._experts:
            expert.stale_read_hook = self._on_stale_cache_read
        self._weights_cache = {}
        self._weights_loaded = False
        self._visibility_learner = None
        _LOGGER.debug(
            "WeatherExpertBlender initialized with %d experts",
            len(
                [
                    enabled_expert
                    for enabled_expert in self._experts
                    if self._is_expert_enabled(enabled_expert)
                ]
            ),
        )

    def _on_stale_cache_read(self) -> None:
        self._stale_cache_reads += 1

    def reset_stale_cache_reads(self) -> None:
        """Clear stale-cache diagnostics before a refresh cycle."""
        self._stale_cache_reads = 0

    def had_stale_cache_reads(self) -> bool:
        """True when any expert rejected a row for TTL during this cycle."""
        return self._stale_cache_reads > 0

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
            weight_rows = await self.fetch_all(
                "SELECT cloud_type, expert_name, weight\n                   FROM weather_expert_weights\n                   WHERE learning_contract_version = ?",
                (WEATHER_EXPERT_STATIC_CONTRACT_VERSION,),
            )
            self._weights_cache.clear()
            for database_row in weight_rows:
                cloud_type_name = database_row[0]
                expert_name = database_row[1]
                weight = database_row[2]
                if cloud_type_name not in self._weights_cache:
                    self._weights_cache[cloud_type_name] = {}
                self._weights_cache[cloud_type_name][expert_name] = weight
            for cloud_type_name, default_weights in DEFAULT_EXPERT_WEIGHTS.items():
                if cloud_type_name not in self._weights_cache:
                    self._weights_cache[cloud_type_name] = default_weights.copy()
                else:
                    for default_expert_name, weight in default_weights.items():
                        if (
                            default_expert_name
                            not in self._weights_cache[cloud_type_name]
                        ):
                            self._weights_cache[cloud_type_name][
                                default_expert_name
                            ] = weight
            self._weights_loaded = True
            _LOGGER.debug("Loaded expert weights from database")
        except Exception as error:
            _LOGGER.warning("Error loading weights from database: %s", error)
            self._weights_cache = {
                cloud: weights.copy()
                for cloud, weights in (DEFAULT_EXPERT_WEIGHTS.items if True else None)()
            }
            self._weights_loaded = True
        if True:
            return

    def _weight_rows(self) -> list[tuple[str, str, float]]:
        """Return a stable snapshot of all learned expert weights."""
        return [
            (cloud_type_name, expert_name, weight)
            for cloud_type_name, expert_weights in self._weights_cache.items()
            for expert_name, weight in expert_weights.items()
        ]

    async def _save_weights_to_db(self) -> None:
        """Save current weights in one short atomic database transaction."""
        transaction_deadline = monotonic() + WEATHER_EXPERT_PERSISTENCE_TIMEOUT_SECONDS
        try:
            async with self.db.transaction(deadline=transaction_deadline):
                await self.db.executemany(
                    WEATHER_EXPERT_WEIGHT_UPSERT_SQL, self._weight_rows()
                )
            _LOGGER.debug("Saved expert weights to database")
        except Exception:
            _LOGGER.exception("Error saving weights to database")
            raise

    def _get_weights_for_cloud_type(self, cloud_type: CloudType) -> dict[str, float]:
        """Get weights for specific cloud type. @zara

        V16.1: For FOG/FOG_LIGHT, uses learned visibility weights from
        VisibilityLearner if available, merged with defaults.
        """
        cloud_type_name = cloud_type.value
        if (
            cloud_type in (CloudType.FOG, CloudType.FOG_LIGHT)
            and self._visibility_learner
        ):
            try:
                learned_fog_weights = self._visibility_learner.get_fog_weight(
                    cloud_type_name
                )
                if learned_fog_weights:
                    merged_weights = DEFAULT_EXPERT_WEIGHTS.get(
                        cloud_type_name, {}
                    ).copy()
                    if "bright_sky" in learned_fog_weights:
                        merged_weights["bright_sky"] = learned_fog_weights["bright_sky"]
                    if "pirate_weather" in learned_fog_weights:
                        merged_weights["pirate_weather"] = learned_fog_weights[
                            "pirate_weather"
                        ]
                    cloud_expert_reduction = 0.3
                    for reduced_expert_name in (
                        "open_meteo",
                        "wttr_in",
                        "ecmwf_layers",
                    ):
                        if reduced_expert_name in merged_weights:
                            merged_weights[
                                reduced_expert_name
                            ] *= cloud_expert_reduction
                    total_weight = sum(merged_weights.values())
                    if total_weight > 0:
                        merged_weights = {
                            expert_name: weight / total_weight
                            for expert_name, weight in merged_weights.items()
                        }
                    return merged_weights
            except Exception as error:
                _LOGGER.debug(
                    "Error getting visibility weights for %s: %s",
                    cloud_type_name,
                    error,
                )
        return self._weights_cache.get(
            cloud_type_name, DEFAULT_EXPERT_WEIGHTS.get(cloud_type_name, {})
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
        cloud_type = classify_cloud_type(
            cloud_low, cloud_mid, cloud_high, fallback_cloud, visibility_m
        )
        expert_weights = self._get_weights_for_cloud_type(cloud_type)
        expert_clouds = {}
        for expert in self._experts:
            if not self._is_expert_enabled(expert):
                continue
            try:
                cloud_cover = await expert.get_cloud_cover(date, hour)
                if cloud_cover is not None:
                    expert_clouds[expert.name] = cloud_cover
            except Exception as error:
                _LOGGER.debug("Expert %s error: %s", expert.name, error)
                continue
        if not expert_clouds:
            fallback_value = fallback_cloud if fallback_cloud is not None else 50.0
            return (
                fallback_value,
                {
                    "cloud_type": cloud_type.value,
                    "experts": {},
                    "weights_used": {},
                    "fallback": True,
                },
            )
        total_weight = 0.0
        weighted_cloud_sum = 0.0
        weights_used = {}
        for expert_name, expert_cloud_cover in expert_clouds.items():
            weight = expert_weights.get(expert_name, 0.0)
            if weight > 0:
                weighted_cloud_sum += expert_cloud_cover * weight
                total_weight += weight
                weights_used[expert_name] = weight
        if total_weight > 0:
            blended_cloud_cover = weighted_cloud_sum / total_weight
        else:
            blended_cloud_cover = sum(expert_clouds.values()) / len(expert_clouds)
        blended_cloud_cover = max(0.0, min(100.0, blended_cloud_cover))
        blend_info = {
            "cloud_type": cloud_type.value,
            "experts": expert_clouds,
            "weights_used": weights_used,
            "total_weight": total_weight,
            "blended": round(blended_cloud_cover, 1),
            "visibility_m": visibility_m,
        }
        return (round(blended_cloud_cover, 1), blend_info)

    async def get_bright_sky_solar_wm2(self, date: str, hour: int) -> Optional[float]:
        """Return Bright Sky solar radiation in W/m² when a value exists."""
        try:
            return await self._bright_sky_expert.get_solar_wm2(date, hour)
        except Exception:
            return

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
        blended_cloud_cover, blend_info = await self.get_blended_cloud_cover(
            date, hour, cloud_low, cloud_mid, cloud_high, fallback_cloud
        )
        cloud_type = classify_cloud_type(
            cloud_low, cloud_mid, cloud_high, fallback_cloud
        )
        path_b_transmission = cloud_to_transmission(blended_cloud_cover, cloud_type)
        path_a_transmission = 100.0 - blended_cloud_cover
        path_b_weight = 1.0 - path_a_weight
        final_transmission = (
            path_a_transmission * path_a_weight + path_b_transmission * path_b_weight
        )
        dual_info = {
            **blend_info,
            "path_a_transmission": round(path_a_transmission, 1),
            "path_b_transmission": round(path_b_transmission, 1),
            "path_a_weight": path_a_weight,
            "final_transmission": round(final_transmission, 1),
            "final_cloud": round(100.0 - final_transmission, 1),
        }
        return (round(final_transmission, 1), dual_info)

    async def update_expert_weight(
        self,
        cloud_type: CloudType,
        expert_name: str,
        mae: float,
        *,
        persist: bool = True,
    ) -> bool:
        """Update expert weight based on MAE using smoothed learning. @zara"""
        if not WEATHER_EXPERT_IMMUTABLE_LEARNING_ENABLED:
            return False
        if mae < LEARNING_MIN_ERROR:
            return False
        previous_weights = deepcopy(self._weights_cache) if persist else None
        cloud_type_name = cloud_type.value
        if cloud_type_name not in self._weights_cache:
            self._weights_cache[cloud_type_name] = DEFAULT_EXPERT_WEIGHTS.get(
                cloud_type_name, {}
            ).copy()
        if expert_name not in self._weights_cache[cloud_type_name]:
            return False
        previous_weight = self._weights_cache[cloud_type_name][expert_name]
        accuracy_score = 1.0 / (1.0 + mae / 100.0)
        target_weight = accuracy_score
        smoothed_weight = (
            previous_weight * (1 - LEARNING_SMOOTHING_FACTOR)
            + target_weight * LEARNING_SMOOTHING_FACTOR
        )
        smoothed_weight = max(0.05, min(0.95, smoothed_weight))
        self._weights_cache[cloud_type_name][expert_name] = smoothed_weight
        total_weight = sum(self._weights_cache[cloud_type_name].values())
        if total_weight > 0:
            for normalization_expert_name in self._weights_cache[cloud_type_name]:
                self._weights_cache[cloud_type_name][
                    normalization_expert_name
                ] /= total_weight
        if persist:
            try:
                await self._save_weights_to_db()
            except BaseException:
                self._weights_cache = previous_weights
                raise
        return True if True else None

    def get_blend_stats(self) -> dict[str, Any]:
        """Get blending statistics. @zara"""
        return {
            "experts_enabled": [
                expert.name
                for expert in self._experts
                if self._is_expert_enabled(expert)
            ],
            "weights_loaded": self._weights_loaded,
            "cloud_types_configured": list(self._weights_cache.keys()),
        }

    async def get_expert_db_status(self) -> dict[str, Any]:
        """Get database status for all experts with data counts. @zara"""
        today_str = dt_util.now().strftime("%Y-%m-%d")
        expert_status = {}
        try:
            database_row = await self.fetch_one(
                "SELECT COUNT(*), MAX(fetched_at) FROM weather_cache_bright_sky\n                   WHERE forecast_date >= ?",
                (today_str,),
            )
            expert_status["bright_sky"] = {
                "hours_in_db": database_row[0] if database_row else 0,
                "last_fetched": database_row[1] if database_row else None,
                "healthy": self._bright_sky_expert.is_healthy,
            }
        except Exception as error:
            expert_status["bright_sky"] = {"error": str(error)}
        try:
            database_row = await self.fetch_one(
                "SELECT COUNT(*), MAX(fetched_at) FROM weather_cache_wttr_in\n                   WHERE forecast_date >= ?",
                (today_str,),
            )
            expert_status["wttr_in"] = {
                "hours_in_db": database_row[0] if database_row else 0,
                "last_fetched": database_row[1] if database_row else None,
                "healthy": self._wttr_expert.is_healthy,
            }
        except Exception as error:
            expert_status["wttr_in"] = {"error": str(error)}
        try:
            if self._pirate_weather_expert._enabled:
                database_row = await self.fetch_one(
                    "SELECT COUNT(*), MAX(fetched_at) FROM weather_cache_pirate_weather\n                       WHERE forecast_date >= ?",
                    (today_str,),
                )
                expert_status["pirate_weather"] = {
                    "hours_in_db": database_row[0] if database_row else 0,
                    "last_fetched": database_row[1] if database_row else None,
                    "healthy": self._pirate_weather_expert.is_healthy,
                    "enabled": True,
                }
            else:
                expert_status["pirate_weather"] = {
                    "enabled": False,
                    "reason": "no_api_key",
                }
        except Exception as error:
            expert_status["pirate_weather"] = {"error": str(error)}
        try:
            database_row = await self.fetch_one(
                "SELECT COUNT(*) FROM weather_forecast WHERE forecast_date >= ?",
                (today_str,),
            )
            expert_status["open_meteo"] = {
                "hours_in_db": database_row[0] if database_row else 0,
                "source": "weather_forecast table",
            }
        except Exception as error:
            expert_status["open_meteo"] = {"error": str(error)}
        return expert_status if True else None

    def set_visibility_learner(self, learner) -> None:
        "Set the VisibilityLearner reference for FOG/FOG_LIGHT weight learning. @zara V16.1\n\nThis allows the blender to use learned visibility weights\ninstead of static defaults for fog conditions.\n\nArgs:\n    learner: VisibilityLearner instance\n"
        self._visibility_learner = learner
        _LOGGER.debug("VisibilityLearner connected to WeatherExpertBlender")


class WeatherExpertLearner(DataManagerIO):
    """Learns expert weights from actual IST (Irradiance-Sunshine-Time) data. @zara

    Compares forecasts vs actuals and updates expert weights accordingly.
    """

    learning_enabled = WEATHER_EXPERT_IMMUTABLE_LEARNING_ENABLED
    learning_disabled_reason = WEATHER_EXPERT_LEARNING_DISABLED_REASON

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
        _LOGGER.debug("WeatherExpertLearner initialized")

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
        if not self.learning_enabled:
            _LOGGER.info(
                "Weather expert learning skipped: %s", self.learning_disabled_reason
            )
            return False
        previous_weights = deepcopy(self._blender._weights_cache)
        try:
            actual_rows = await self.fetch_all(
                "SELECT wa.hour, wa.cloud_cover_percent,\n                          COALESCE(hp.exclude_from_learning, 0),\n                          COALESCE(hp.mppt_throttled, 0),\n                          COALESCE(hp.inverter_clipped, 0),\n                          COALESCE(wa.snow_event_detected, 0)\n                   FROM hourly_weather_actual wa\n                   LEFT JOIN hourly_predictions hp ON hp.target_date = wa.date AND hp.target_hour = wa.hour\n                   WHERE wa.date = ? AND wa.cloud_cover_percent IS NOT NULL",
                (date_str,),
            )
            if len(actual_rows) < LEARNING_MIN_HOURS:
                _LOGGER.debug(
                    "Not enough data for learning: %d hours (need %d)",
                    len(actual_rows),
                    LEARNING_MIN_HOURS,
                )
                return False
            errors_by_cloud_type = {}
            hours_by_cloud_type = {}
            for actual_row in actual_rows:
                forecast_hour = actual_row[0]
                actual_cloud_cover = actual_row[1]
                excluded_from_learning = actual_row[2]
                mppt_throttled = actual_row[3]
                inverter_clipped = actual_row[4]
                snow_event_detected = actual_row[5]
                if excluded_from_learning or mppt_throttled or inverter_clipped:
                    _LOGGER.info(
                        "Skipping weather expert learning for %s hour %d due to curtailment/exclusion",
                        date_str,
                        forecast_hour,
                    )
                    continue
                cloud_type = cloud_type_from_actual(
                    actual_cloud_cover, bool(snow_event_detected)
                )
                cloud_type_name = cloud_type.value
                hours_by_cloud_type[cloud_type_name] = (
                    hours_by_cloud_type.get(cloud_type_name, 0) + 1
                )
                if cloud_type_name not in errors_by_cloud_type:
                    errors_by_cloud_type[cloud_type_name] = {}
                for expert in self._blender._experts:
                    if not self._blender._is_expert_enabled(expert):
                        continue
                    try:
                        predicted_cloud_cover = await expert.get_cloud_cover(
                            date_str, forecast_hour
                        )
                        if predicted_cloud_cover is not None:
                            absolute_error = abs(
                                predicted_cloud_cover - actual_cloud_cover
                            )
                            if expert.name not in errors_by_cloud_type[cloud_type_name]:
                                errors_by_cloud_type[cloud_type_name][expert.name] = []
                            errors_by_cloud_type[cloud_type_name][expert.name].append(
                                absolute_error
                            )
                    except Exception:
                        continue
                continue
            updated_cloud_types = 0
            learning_rows = []
            for cloud_type_name, errors_by_expert in errors_by_cloud_type.items():
                cloud_type_hours = hours_by_cloud_type.get(cloud_type_name, 0)
                if cloud_type_hours < 2:
                    continue
                cloud_type = CloudType(cloud_type_name)
                bucket_updated = False
                for expert_name, expert_errors in errors_by_expert.items():
                    if len(expert_errors) < 2:
                        continue
                    mean_absolute_error = sum(expert_errors) / len(expert_errors)
                    weight_updated = await self._blender.update_expert_weight(
                        cloud_type, expert_name, mean_absolute_error, persist=False
                    )
                    if not weight_updated:
                        continue
                    bucket_updated = True
                    updated_weight = self._blender._weights_cache[cloud_type_name][
                        expert_name
                    ]
                    learning_rows.append(
                        (
                            date_str,
                            cloud_type_name,
                            expert_name,
                            mean_absolute_error,
                            updated_weight,
                        )
                    )
                if bucket_updated:
                    updated_cloud_types += 1
            if learning_rows:
                await self._persist_learning_batch(learning_rows)
            _LOGGER.info(
                "Learned from %s: %d hours across %d cloud types (buckets: %s)",
                date_str,
                len(actual_rows),
                updated_cloud_types,
                ", ".join(
                    (
                        f"{cloud_type_name}:{hour_count}h"
                        for cloud_type_name, hour_count in hours_by_cloud_type.items()
                    )
                ),
            )
            if self._visibility_learner:
                try:
                    visibility_result = await self._visibility_learner.learn_from_day(
                        date_str
                    )
                    if visibility_result and visibility_result.get("success"):
                        fog_hours = visibility_result.get("fog_hours_detected", 0)
                        light_fog_hours = visibility_result.get(
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
            else:
                return True
        except BaseException:
            self._blender._weights_cache = previous_weights
            _LOGGER.exception("Error learning from day %s", date_str)
            raise

    async def _persist_learning_batch(
        self, learning_rows: list[tuple[str, str, str, float, float]]
    ) -> None:
        """Persist weights and their daily learning evidence atomically."""
        transaction_deadline = monotonic() + WEATHER_EXPERT_PERSISTENCE_TIMEOUT_SECONDS
        async with self.db.transaction(deadline=transaction_deadline):
            await self.db.executemany(
                WEATHER_EXPERT_WEIGHT_UPSERT_SQL, self._blender._weight_rows()
            )
            await self.db.executemany(WEATHER_EXPERT_LEARNING_UPSERT_SQL, learning_rows)
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
        snow_result = {
            "success": False,
            "date": date_str,
            "snow_predictions": [],
            "total_snow_predicted": 0,
            "snow_confirmed": 0,
            "accuracy": 0.0,
        }
        try:
            minimum_radiation_wm2 = 100
            maximum_snow_production_kwh = 0.02
            snow_predictions = []
            forecast_rows = await self.fetch_all(
                "SELECT hour, temperature, rain, solar_radiation_wm2\n                   FROM weather_forecast\n                   WHERE forecast_date = ?",
                (date_str,),
            )
            forecasts_by_hour = {}
            for database_row in forecast_rows:
                forecast_hour = database_row[0]
                forecasts_by_hour[forecast_hour] = {
                    "temperature": database_row[1],
                    "precipitation_mm": database_row[2] or 0,
                    "solar_radiation_wm2": database_row[3] or 0,
                }
            for prediction in hourly_predictions:
                forecast_hour = prediction.get("target_hour")
                if forecast_hour is None:
                    continue
                hour_forecast = forecasts_by_hour.get(forecast_hour, {})
                if not hour_forecast:
                    continue
                temperature_c = hour_forecast.get("temperature", 10)
                precipitation_mm = hour_forecast.get("precipitation_mm", 0)
                solar_radiation_wm2 = hour_forecast.get("solar_radiation_wm2", 0)
                hour_actual = hourly_actuals.get(str(forecast_hour), {})
                condition = hour_actual.get("condition", "").lower()
                snow_predicted = "snow" in condition or (
                    precipitation_mm > 0.5 and temperature_c < 2.0
                )
                if not snow_predicted:
                    continue
                if solar_radiation_wm2 < minimum_radiation_wm2:
                    continue
                actual_production_kwh = prediction.get("actual_kwh", 0) or 0
                snow_confirmed = (
                    actual_production_kwh < maximum_snow_production_kwh
                    or hour_actual.get("snow_covered_panels", False)
                )
                snow_predictions.append(
                    {
                        "hour": forecast_hour,
                        "predicted": True,
                        "confirmed": snow_confirmed,
                        "expected_radiation": solar_radiation_wm2,
                        "actual_production": actual_production_kwh,
                        "condition": condition,
                        "temp": temperature_c,
                        "precip": precipitation_mm,
                    }
                )
                continue
            snow_result["snow_predictions"] = snow_predictions
            snow_result["total_snow_predicted"] = len(snow_predictions)
            if snow_predictions:
                confirmed_count = sum(
                    (
                        1
                        for snow_prediction in snow_predictions
                        if snow_prediction["confirmed"]
                    )
                )
                prediction_count = len(snow_predictions)
                accuracy = (
                    confirmed_count / prediction_count if prediction_count > 0 else 0.0
                )
                snow_result["snow_confirmed"] = confirmed_count
                snow_result["accuracy"] = round(accuracy, 3)
                await self._update_snow_stats(confirmed_count, prediction_count)
                snow_result["success"] = True
                _LOGGER.debug(
                    "Snow prediction learning for %s: %d/%d confirmed (%.1f%%)",
                    date_str,
                    confirmed_count,
                    prediction_count,
                    accuracy * 100,
                )
            else:
                snow_result["success"] = False
                _LOGGER.debug("No snow predictions for %s", date_str)
            return snow_result if True else None
        except Exception as error:
            _LOGGER.error(
                "Error in snow prediction learning for %s: %s", date_str, error
            )
            return snow_result if True else None

    async def _update_snow_stats(self, correct: int, total: int) -> None:
        """Update global snow prediction statistics in DB. @zara"""
        try:
            database_row = await self.fetch_one(
                "SELECT total_predictions, correct_predictions FROM weather_expert_snow_stats WHERE id = 1"
            )
            if database_row:
                previous_total = database_row[0] or 0
                previous_correct = database_row[1] or 0
            else:
                previous_total = 0
                previous_correct = 0
            updated_total = previous_total + total
            updated_correct = previous_correct + correct
            updated_accuracy = (
                updated_correct / updated_total if updated_total > 0 else 0.0
            )
            await self.execute_query(
                "INSERT INTO weather_expert_snow_stats (id, total_predictions, correct_predictions, accuracy, last_updated)\n                   VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)\n                   ON CONFLICT(id) DO UPDATE SET\n                       total_predictions = excluded.total_predictions,\n                       correct_predictions = excluded.correct_predictions,\n                       accuracy = excluded.accuracy,\n                       last_updated = CURRENT_TIMESTAMP",
                (updated_total, updated_correct, updated_accuracy),
            )
            _LOGGER.debug(
                "Snow stats updated: %d/%d correct (%.1f%% accuracy)",
                updated_correct,
                updated_total,
                updated_accuracy * 100,
            )
        except Exception as error:
            _LOGGER.error("Error updating snow stats: %s", error)
        return None if True else None
