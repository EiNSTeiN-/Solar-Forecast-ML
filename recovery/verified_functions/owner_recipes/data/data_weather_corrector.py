# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
import math
from pathlib import Path
from typing import Any, Optional
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .db_manager import DatabaseManager
from .data_io import DataManagerIO
from .data_weather_radiation_ensemble import (
    RadiationSources,
    SNAPSHOT_KIND_ENSEMBLE_COMPOSE,
    align_provider_radiation_to_interval_start,
    apply_terminal_kalman_ghi,
    blend_static_with_mlp_residual,
    compose_ensemble_radiation,
    ensemble_compose_source_rows,
    open_meteo_base_source_identity,
    shrink_ensemble_solar_precision_factor,
    should_apply_coherence_cap,
    should_apply_kalman,
    should_apply_solar_precision_factor,
    snapshot_target_datetime,
)
from ..const import (
    CONF_SOLAR_RADIATION_SENSOR,
    DOMAIN,
    WINTER_MONTHS,
    WINTER_CLOUD_PENALTY_FACTOR,
    WINTER_LOW_SUN_THRESHOLD,
)


class WeatherForecastCorrector(DataManagerIO):

    async def _load_raw_forecasts(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Load RAW forecast data from Open-Meteo cache (NOT corrected!). @zara

        V16.1 FIX: Reads from weather_cache_open_meteo to avoid circular dependency.
        The corrected forecasts will be written to weather_forecast.
        """
        try:
            _var_var_51 = dt_util.now().date().isoformat()
            try:
                _var_var_52 = await self.fetch_all(
                    "SELECT forecast_date, hour, temperature, humidity, cloud_cover,\n                              precipitation, wind_speed, pressure, direct_radiation, diffuse_radiation,\n                              ghi, cloud_cover_low, cloud_cover_mid,\n                              cloud_cover_high, visibility_m, weather_code, snowfall, rain,\n                              ghi_icon_d2, direct_radiation_icon_d2, diffuse_radiation_icon_d2,\n                              ghi_icon_eu, direct_radiation_icon_eu, diffuse_radiation_icon_eu,\n                              source, ghi_source_identity, visibility_source\n                       FROM weather_cache_open_meteo\n                       WHERE forecast_date >= ?\n                       ORDER BY forecast_date, hour",
                    (_var_var_51,),
                )
            except Exception:
                _var_var_52 = await self.fetch_all(
                    "SELECT forecast_date, hour, temperature, humidity, cloud_cover,\n                              precipitation, wind_speed, pressure, direct_radiation, diffuse_radiation,\n                              ghi, cloud_cover_low, cloud_cover_mid,\n                              cloud_cover_high, visibility_m, weather_code, snowfall, rain\n                       FROM weather_cache_open_meteo\n                       WHERE forecast_date >= ?\n                       ORDER BY forecast_date, hour",
                    (_var_var_51,),
                )
            _var_var_53 = []
            for _var_var_54 in _var_var_52:
                _var_var_53.append(
                    {
                        "date": str(_var_var_54[0]),
                        "hour": int(_var_var_54[1]),
                        "temperature": _var_var_54[2],
                        "humidity": _var_var_54[3],
                        "cloud_cover": _var_var_54[4],
                        "precipitation": _var_var_54[5],
                        "wind_speed": _var_var_54[6],
                        "pressure": _var_var_54[7],
                        "direct_radiation": _var_var_54[8],
                        "diffuse_radiation": _var_var_54[9],
                        "ghi": _var_var_54[10],
                        "cloud_cover_low": _var_var_54[11],
                        "cloud_cover_mid": _var_var_54[12],
                        "cloud_cover_high": _var_var_54[13],
                        "visibility_m": _var_var_54[14],
                        "weather_code": _var_var_54[15],
                        "snowfall": _var_var_54[16],
                        "rain": _var_var_54[17],
                        "ghi_icon_d2": (
                            _var_var_54[18] if len(_var_var_54) > 18 else None
                        ),
                        "direct_radiation_icon_d2": (
                            _var_var_54[19] if len(_var_var_54) > 19 else None
                        ),
                        "diffuse_radiation_icon_d2": (
                            _var_var_54[20] if len(_var_var_54) > 20 else None
                        ),
                        "ghi_icon_eu": (
                            _var_var_54[21] if len(_var_var_54) > 21 else None
                        ),
                        "direct_radiation_icon_eu": (
                            _var_var_54[22] if len(_var_var_54) > 22 else None
                        ),
                        "diffuse_radiation_icon_eu": (
                            _var_var_54[23] if len(_var_var_54) > 23 else None
                        ),
                        "source": _var_var_54[24] if len(_var_var_54) > 24 else None,
                        "ghi_source_identity": (
                            _var_var_54[25]
                            if len(_var_var_54) > 25 and _var_var_54[25]
                            else open_meteo_base_source_identity(
                                _var_var_54[24] if len(_var_var_54) > 24 else None
                            )
                        ),
                        "visibility_source": (
                            _var_var_54[26] if len(_var_var_54) > 26 else None
                        ),
                    }
                )
                continue
            _var_var_55 = {}
            for _var_var_26 in align_provider_radiation_to_interval_start(_var_var_53):
                _var_var_40 = str(_var_var_26.pop("date"))
                _var_var_56 = str(_var_var_26.pop("hour"))
                _var_var_55.setdefault(_var_var_40, {})[_var_var_56] = _var_var_26
            return _var_var_55
        except Exception as _var_var_50:
            _LOGGER.error("Error loading raw forecasts: %s", _var_var_50)
            return {}

    async def _load_correction_factors(self) -> dict[str, Any]:
        """Load precision correction factors including hourly + weather-specific. @zara

        V16.1: Now loads three types of factors:
        - Daily rolling averages (baseline)
        - Hourly factors (hours 7-17) for solar and clouds
        - Weather-specific factors (clear/cloudy days)
        """
        _var_var_59 = {
            "temperature": 0.0,
            "solar_radiation_wm2": 1.0,
            "clouds": 1.0,
            "wind": 1.0,
            "humidity": 1.0,
            "rain": 1.0,
            "pressure": 0.0,
        }
        try:
            _var_var_54 = await self.fetch_one(
                "SELECT temperature_factor, solar_factor, cloud_factor,\n                          wind_factor, humidity_factor, rain_factor,\n                          pressure_factor, sample_days, confidence\n                   FROM weather_precision_factors\n                   WHERE id = 1"
            )
            _var_var_34 = _var_var_59.copy()
            _var_var_60 = 0
            _var_var_61 = 0.0
            if _var_var_54:
                _var_var_34 = {
                    "temperature": _var_var_54[0] or 0.0,
                    "solar_radiation_wm2": _var_var_54[1] or 1.0,
                    "clouds": _var_var_54[2] or 1.0,
                    "wind": _var_var_54[3] or 1.0,
                    "humidity": _var_var_54[4] or 1.0,
                    "rain": _var_var_54[5] or 1.0,
                    "pressure": _var_var_54[6] or 0.0,
                }
                _var_var_60 = _var_var_54[7] or 0
                _var_var_61 = _var_var_54[8] or 0.0
            _var_var_35 = {"solar_radiation_wm2": {}, "clouds": {}}
            _var_var_62 = await self.fetch_all(
                "SELECT hour, factor_type, factor_value, sample_count, confidence\n                   FROM weather_precision_hourly_factors\n                   WHERE factor_type IN ('solar_radiation_wm2', 'clouds')"
            )
            for _var_var_63 in _var_var_62:
                _var_var_42, _var_var_64, _var_var_65, _var_var_66, _var_var_67 = (
                    _var_var_63
                )
                if _var_var_64 in _var_var_35:
                    _var_var_35[_var_var_64][str(_var_var_42)] = {
                        "factor": _var_var_65,
                        "samples": _var_var_66,
                        "confidence": _var_var_67 or 0.0,
                    }
            _var_var_36 = {"clear": {}, "cloudy": {}}
            _var_var_68 = await self.fetch_all(
                "SELECT weather_type, factor_type, factor_value, confidence\n                   FROM weather_precision_weather_specific"
            )
            for _var_var_69 in _var_var_68:
                _var_var_46, _var_var_64, _var_var_65, _var_var_70 = _var_var_69
                if _var_var_46 in _var_var_36 and _var_var_70 and (_var_var_70 >= 0.3):
                    _var_var_36[_var_var_46][_var_var_64] = _var_var_65
            return {
                "factors": _var_var_34,
                "hourly_factors": _var_var_35,
                "weather_factors": _var_var_36,
                "sample_days": _var_var_60,
                "solar_calibration": {
                    "local_observation_available": self._has_available_local_solar_observation(),
                    "sample_days": _var_var_60,
                    "confidence": _var_var_61,
                },
            }
        except Exception as _var_var_50:
            _LOGGER.debug("Using default correction factors: %s", _var_var_50)
            return {
                "factors": _var_var_59,
                "hourly_factors": {"solar_radiation_wm2": {}, "clouds": {}},
                "weather_factors": {"clear": {}, "cloudy": {}},
                "sample_days": 0,
                "solar_calibration": {
                    "local_observation_available": False,
                    "sample_days": 0,
                    "confidence": 0.0,
                },
            }
