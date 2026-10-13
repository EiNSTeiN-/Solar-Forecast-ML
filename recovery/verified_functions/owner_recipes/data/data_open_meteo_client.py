# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class OpenMeteoClient(DataManagerIO):

    async def _load_from_database(self) -> bool:
        """Load forecast data from database into memory cache. @zara"""
        try:
            _var_var_21 = dt_util.now().date().isoformat()
            try:
                _var_var_22 = await self.fetch_all(
                    "SELECT forecast_date, hour, temperature, humidity, cloud_cover,\n                              precipitation, wind_speed, pressure, direct_radiation, diffuse_radiation,\n                              ghi, cloud_cover_low, cloud_cover_mid,\n                              cloud_cover_high, visibility_m, weather_code, snowfall, rain,\n                              fetched_at, source, ghi_icon_d2, direct_radiation_icon_d2,\n                              diffuse_radiation_icon_d2, ghi_icon_eu, direct_radiation_icon_eu,\n                              diffuse_radiation_icon_eu, ghi_source_identity, visibility_source\n                       FROM weather_cache_open_meteo\n                       WHERE forecast_date >= ?\n                       ORDER BY forecast_date, hour",
                    (_var_var_21,),
                )
            except Exception:
                _var_var_22 = await self.fetch_all(
                    "SELECT forecast_date, hour, temperature, humidity, cloud_cover,\n                              precipitation, wind_speed, pressure, direct_radiation, diffuse_radiation,\n                              ghi, cloud_cover_low, cloud_cover_mid,\n                              cloud_cover_high, visibility_m, weather_code, snowfall, rain,\n                              fetched_at, source\n                       FROM weather_cache_open_meteo\n                       WHERE forecast_date >= ?\n                       ORDER BY forecast_date, hour",
                    (_var_var_21,),
                )
            if not _var_var_22:
                self._set_forecast_status("failed", "database_cache", "database_empty")
                return False
            _var_var_23 = []
            _var_var_24 = None
            for _var_var_16 in _var_var_22:
                _var_var_4 = {
                    "date": str(_var_var_16[0]),
                    "hour": _var_var_16[1],
                    "temperature": _var_var_16[2],
                    "humidity": _var_var_16[3],
                    "cloud_cover": _var_var_16[4],
                    "precipitation": _var_var_16[5],
                    "wind_speed": _var_var_16[6],
                    "pressure": _var_var_16[7],
                    "direct_radiation": _var_var_16[8] or 0,
                    "diffuse_radiation": _var_var_16[9] or 0,
                    "ghi": _var_var_16[10]
                    or (_var_var_16[8] or 0) + (_var_var_16[9] or 0),
                    "cloud_cover_low": _var_var_16[11],
                    "cloud_cover_mid": _var_var_16[12],
                    "cloud_cover_high": _var_var_16[13],
                    "visibility_m": _var_var_16[14],
                    "weather_code": _var_var_16[15],
                    "snowfall": _var_var_16[16],
                    "rain": _var_var_16[17],
                    "ghi_icon_d2": _var_var_16[20] if len(_var_var_16) > 20 else None,
                    "direct_radiation_icon_d2": (
                        _var_var_16[21] if len(_var_var_16) > 21 else None
                    ),
                    "diffuse_radiation_icon_d2": (
                        _var_var_16[22] if len(_var_var_16) > 22 else None
                    ),
                    "ghi_icon_eu": _var_var_16[23] if len(_var_var_16) > 23 else None,
                    "direct_radiation_icon_eu": (
                        _var_var_16[24] if len(_var_var_16) > 24 else None
                    ),
                    "diffuse_radiation_icon_eu": (
                        _var_var_16[25] if len(_var_var_16) > 25 else None
                    ),
                    "source": (
                        _var_var_16[19]
                        if len(_var_var_16) > 19 and _var_var_16[19]
                        else "database"
                    ),
                    "ghi_source_identity": (
                        _var_var_16[26]
                        if len(_var_var_16) > 26 and _var_var_16[26]
                        else open_meteo_base_source_identity(
                            _var_var_16[19] if len(_var_var_16) > 19 else None
                        )
                    ),
                    "visibility_source": (
                        _var_var_16[27] if len(_var_var_16) > 27 else None
                    ),
                }
                _var_var_23.append(_var_var_4)
                _var_var_25 = self._parse_cache_timestamp(
                    _var_var_16[18] if len(_var_var_16) > 18 else None
                )
                if _var_var_25:
                    if _var_var_24 is None or _var_var_25 > _var_var_24:
                        _var_var_24 = _var_var_25
            _var_var_23 = align_provider_radiation_to_interval_start(_var_var_23)
            self._cache["hourly_forecast"] = _var_var_23
            self._cache_time = _var_var_24 or dt_util.now()
            if self._has_required_horizon(_var_var_23):
                self._set_forecast_status(
                    "ok", "database_cache", "loaded_from_database"
                )
            else:
                self._set_forecast_status(
                    "degraded", "database_cache", "database_cache_incomplete_horizon"
                )
            _LOGGER.debug(
                "Loaded %d hours from database into Open-Meteo cache", len(_var_var_23)
            )
            return True
        except Exception as _var_var_26:
            _LOGGER.warning(
                "Error loading Open-Meteo data from database: %s", _var_var_26
            )
            return False

    async def _save_to_database(self, hourly_data: list[dict[str, Any]]) -> bool:
        """Save forecast data to database. @zara"""
        try:
            _var_var_27 = dt_util.now()
            _var_var_28 = _var_var_27.isoformat()
            for _var_var_4 in hourly_data:
                _var_var_2 = _var_var_4.get("date")
                _var_var_5 = _var_var_4.get("hour")
                if _var_var_2 is None or _var_var_5 is None:
                    continue
                _var_var_29 = _var_var_4.get("direct_radiation")
                _var_var_30 = _var_var_4.get("diffuse_radiation")
                _var_var_31 = _var_var_4.get("ghi")
                if _var_var_31 is None and (
                    _var_var_29 is not None or _var_var_30 is not None
                ):
                    _var_var_31 = (_var_var_29 or 0) + (_var_var_30 or 0)
                if _var_var_31 is not None:
                    _var_var_31 = max(0, min(1600, _var_var_31))
                _var_var_32 = dict(_var_var_4)
                _var_var_32["ghi"] = _var_var_31
                _var_var_33 = int(_var_var_5)
                async with self.db.transaction():
                    await self.db.insert_weather_radiation_snapshot(
                        snapshot_kind=SNAPSHOT_KIND_PROVIDER_FETCH,
                        retrieved_at=_var_var_28,
                        provider_run_at=None,
                        target_datetime=snapshot_target_datetime(
                            str(_var_var_2), _var_var_33, _var_var_27
                        ),
                        target_date=str(_var_var_2),
                        target_hour=_var_var_33,
                        lead_hours=forecast_lead_hours(
                            str(_var_var_2), _var_var_33, _var_var_27
                        ),
                        sources=provider_fetch_source_rows_open_meteo(_var_var_32),
                    )
                    await self.db.execute(
                        "INSERT INTO weather_cache_open_meteo\n                       (forecast_date, hour, temperature, humidity, cloud_cover, precipitation,\n                        wind_speed, pressure, direct_radiation, diffuse_radiation,\n                        ghi, cloud_cover_low, cloud_cover_mid,\n                        cloud_cover_high, visibility_m, weather_code, snowfall, rain,\n                        ghi_icon_d2, direct_radiation_icon_d2, diffuse_radiation_icon_d2,\n                        ghi_icon_eu, direct_radiation_icon_eu, diffuse_radiation_icon_eu,\n                        source, ghi_source_identity, visibility_source, fetched_at)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n                       ON CONFLICT(forecast_date, hour) DO UPDATE SET\n                           temperature = COALESCE(excluded.temperature, temperature),\n                           humidity = COALESCE(excluded.humidity, humidity),\n                           cloud_cover = COALESCE(excluded.cloud_cover, cloud_cover),\n                           precipitation = COALESCE(excluded.precipitation, precipitation),\n                           wind_speed = COALESCE(excluded.wind_speed, wind_speed),\n                           pressure = COALESCE(excluded.pressure, pressure),\n                           direct_radiation = COALESCE(excluded.direct_radiation, direct_radiation),\n                           diffuse_radiation = COALESCE(excluded.diffuse_radiation, diffuse_radiation),\n                           ghi = COALESCE(excluded.ghi, ghi),\n                           cloud_cover_low = COALESCE(excluded.cloud_cover_low, cloud_cover_low),\n                           cloud_cover_mid = COALESCE(excluded.cloud_cover_mid, cloud_cover_mid),\n                           cloud_cover_high = COALESCE(excluded.cloud_cover_high, cloud_cover_high),\n                           visibility_m = COALESCE(excluded.visibility_m, visibility_m),\n                           weather_code = COALESCE(excluded.weather_code, weather_code),\n                           snowfall = COALESCE(excluded.snowfall, snowfall),\n                           rain = COALESCE(excluded.rain, rain),\n                           ghi_icon_d2 = COALESCE(excluded.ghi_icon_d2, ghi_icon_d2),\n                           direct_radiation_icon_d2 = COALESCE(excluded.direct_radiation_icon_d2, direct_radiation_icon_d2),\n                           diffuse_radiation_icon_d2 = COALESCE(excluded.diffuse_radiation_icon_d2, diffuse_radiation_icon_d2),\n                           ghi_icon_eu = COALESCE(excluded.ghi_icon_eu, ghi_icon_eu),\n                           direct_radiation_icon_eu = COALESCE(excluded.direct_radiation_icon_eu, direct_radiation_icon_eu),\n                           diffuse_radiation_icon_eu = COALESCE(excluded.diffuse_radiation_icon_eu, diffuse_radiation_icon_eu),\n                           source = excluded.source,\n                           ghi_source_identity = COALESCE(excluded.ghi_source_identity, ghi_source_identity),\n                           visibility_source = COALESCE(excluded.visibility_source, visibility_source),\n                           fetched_at = CURRENT_TIMESTAMP",
                        (
                            _var_var_2,
                            _var_var_5,
                            _var_var_4.get("temperature"),
                            _var_var_4.get("humidity"),
                            _var_var_4.get("cloud_cover"),
                            _var_var_4.get("precipitation") or 0,
                            _var_var_4.get("wind_speed"),
                            _var_var_4.get("pressure"),
                            _var_var_29,
                            _var_var_30,
                            _var_var_31,
                            _var_var_4.get("cloud_cover_low"),
                            _var_var_4.get("cloud_cover_mid"),
                            _var_var_4.get("cloud_cover_high"),
                            _var_var_4.get("visibility_m"),
                            _var_var_4.get("weather_code"),
                            _var_var_4.get("snowfall"),
                            _var_var_4.get("rain"),
                            _var_var_4.get("ghi_icon_d2"),
                            _var_var_4.get("direct_radiation_icon_d2"),
                            _var_var_4.get("diffuse_radiation_icon_d2"),
                            _var_var_4.get("ghi_icon_eu"),
                            _var_var_4.get("direct_radiation_icon_eu"),
                            _var_var_4.get("diffuse_radiation_icon_eu"),
                            _var_var_4.get("source") or "open-meteo",
                            (
                                _var_var_4.get("ghi_source_identity")
                                or open_meteo_base_source_identity(
                                    _var_var_4.get("source")
                                )
                                if _var_var_31 is not None
                                else None
                            ),
                            _var_var_4.get("visibility_source"),
                        ),
                        auto_commit=False,
                    )
            _LOGGER.debug(
                "Saved %d hours to weather_cache_open_meteo table (RAW)",
                len(hourly_data),
            )
        except Exception as _var_var_26:
            _LOGGER.error("Error saving Open-Meteo data to database: %s", _var_var_26)
            return False
        return True

    async def _fetch_models_from_api(
        self, models: Optional[str], *, endpoint: str, source: str
    ) -> Optional[list[dict[str, Any]]]:
        """Fetch and parse one endpoint without mutating the persistent cache."""
        if not self._check_rate_limit_budget():
            self._last_api_status = 429
            self._last_api_error = "Local rate-limit budget exhausted"
            return
        try:
            _var_var_51 = self._forecast_params(models)
            self._record_api_call()
            self._last_api_status = None
            self._last_retry_after_seconds = None
            async with aiohttp.ClientSession() as _var_var_52:
                async with _var_var_52.get(
                    endpoint,
                    params=_var_var_51,
                    timeout=aiohttp.ClientTimeout(total=OPEN_METEO_TIMEOUT),
                    allow_redirects=False,
                ) as _var_var_53:
                    self._last_api_status = _var_var_53.status
                    if _var_var_53.status != 200:
                        self._last_api_error = f"HTTP {_var_var_53.status}"
                        _var_var_54 = _var_var_53.headers.get("Retry-After")
                        if _var_var_54:
                            try:
                                self._last_retry_after_seconds = max(
                                    0.0, float(_var_var_54)
                                )
                            except (TypeError, ValueError):
                                self._last_retry_after_seconds = None
                        _var_var_55 = (
                            _LOGGER.info
                            if _var_var_53.status in OPEN_METEO_RETRYABLE_STATUSES
                            else _LOGGER.warning
                        )
                        _var_var_55(
                            "Open-Meteo API returned status %d (source=%s models=%s)",
                            _var_var_53.status,
                            source,
                            models,
                        )
                        return
                    _var_var_56 = await _var_var_53.json()
            _var_var_57 = self._parse_hourly_response(_var_var_56, source=source)
            if _var_var_57:
                self._cache["daily"] = _var_var_56.get("daily", {})
                _var_var_50 = [
                    _var_var_58.get("ghi", 0) or 0 for _var_var_58 in _var_var_57
                ]
                _var_var_59 = sum(
                    (
                        1
                        for _var_var_58 in _var_var_57
                        if _var_var_58.get("ghi_icon_d2") is not None
                    )
                )
                _LOGGER.info(
                    "Open-Meteo refresh: %d h (source=%s models=%s, ICON-D2 %d h, GHI %.0f–%.0f W/m²)",
                    len(_var_var_57),
                    source,
                    models,
                    _var_var_59,
                    min(_var_var_50),
                    max(_var_var_50),
                )
            return _var_var_57
        except asyncio.TimeoutError:
            self._last_api_status = None
            self._last_api_error = "Timeout"
            _LOGGER.warning("Open-Meteo API request timed out (source=%s)", source)
            return
        except aiohttp.ClientError as _var_var_26:
            self._last_api_status = None
            self._last_api_error = f"Connection: {_var_var_26}"
            _LOGGER.warning(
                "Open-Meteo API connection error (%s): %s", source, _var_var_26
            )
            return
        except Exception as _var_var_26:
            self._last_api_status = None
            self._last_api_error = f"Error: {_var_var_26}"
            _LOGGER.error(
                "Open-Meteo API error (%s): %s", source, _var_var_26, exc_info=True
            )
            return
