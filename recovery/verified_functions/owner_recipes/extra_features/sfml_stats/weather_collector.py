# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from pathlib import Path
from typing import Any
from collections import defaultdict
from homeassistant.core import HomeAssistant
from .readers.solar_reader import SolarDataReader
from .readers.weather_reader import WeatherDataReader


class WeatherDataCollector:

    async def _load_from_solar_forecast_ml(self) -> list[dict[str, Any]]:
        """Load and convert weather data from Solar Forecast ML database. @zara"""
        try:
            _var_var_21 = Path(self.hass.config.path())
            _var_var_22 = WeatherDataReader(_var_var_21)
            if not _var_var_22.is_available:
                _LOGGER.debug("Solar Forecast ML database not found")
                return []
            _var_var_23 = await _var_var_22.async_get_hourly_weather()
            _var_var_24 = await _var_var_22.async_get_archived_hourly_weather()
            if _var_var_24:
                _var_var_26 = {
                    (_var_var_25.date, _var_var_25.hour): _var_var_25
                    for _var_var_25 in _var_var_24
                }
                _var_var_26.update(
                    {
                        (_var_var_25.date, _var_var_25.hour): _var_var_25
                        for _var_var_25 in _var_var_23
                    }
                )
                _var_var_23 = [
                    _var_var_26[_var_var_27] for _var_var_27 in sorted(_var_var_26)
                ]
            if not _var_var_23:
                _LOGGER.debug("No hourly weather data in database")
                return []
            _var_var_28 = defaultdict(list)
            _var_var_29 = defaultdict(list)
            _var_var_30 = defaultdict(list)
            _var_var_31 = defaultdict(list)
            _var_var_32 = defaultdict(list)
            _var_var_33 = defaultdict(list)
            _var_var_34 = defaultdict(set)
            _var_var_35, _var_var_36, _var_var_37 = _configured_rain_semantics(
                self.hass
            )
            for _var_var_25 in _var_var_23:
                _var_var_38 = _var_var_25.date.isoformat()
                _var_var_34[_var_var_38].add(_var_var_25.hour)
                if _var_var_25.temperature_c is not None:
                    _var_var_28[_var_var_38].append(_var_var_25.temperature_c)
                if _var_var_25.humidity_percent is not None:
                    _var_var_29[_var_var_38].append(_var_var_25.humidity_percent)
                if _var_var_25.wind_speed_ms is not None:
                    _var_var_30[_var_var_38].append(_var_var_25.wind_speed_ms)
                _var_var_2 = _var_var_25.precipitation_semantics
                _var_var_3 = _var_var_25.precipitation_unit
                _var_var_4 = _var_var_25.precipitation_quality
                if _var_var_35 and (not any((_var_var_2, _var_var_3, _var_var_4))):
                    _var_var_2, _var_var_3, _var_var_4 = (
                        _var_var_35,
                        _var_var_36,
                        _var_var_37,
                    )
                if _var_var_25.precipitation_mm is not None or any(
                    (_var_var_2, _var_var_3, _var_var_4)
                ):
                    _var_var_31[_var_var_38].append(
                        (
                            _var_var_25.precipitation_mm,
                            _var_var_2,
                            _var_var_3,
                            _var_var_4,
                        )
                    )
                if _var_var_25.solar_radiation_wm2 is not None:
                    _var_var_32[_var_var_38].append(_var_var_25.solar_radiation_wm2)
                if _var_var_25.cloud_cover_percent is not None:
                    _var_var_33[_var_var_38].append(_var_var_25.cloud_cover_percent)
            _var_var_39 = {}
            try:
                _var_var_21 = Path(self.hass.config.path())
                _var_var_40 = SolarDataReader(_var_var_21, hass=self.hass)
                _var_var_41 = await _var_var_40.async_get_daily_summaries()
                for _var_var_42 in _var_var_41:
                    _var_var_43 = _var_var_42.date.isoformat()
                    _var_var_44 = _var_var_42.actual_total_kwh
                    if _var_var_43 and _var_var_44:
                        _var_var_39[_var_var_43] = _var_var_44
            except Exception as _var_var_45:
                _LOGGER.warning(
                    "Could not load solar summaries from database: %s", _var_var_45
                )
            _var_var_46 = []
            for _var_var_38 in sorted(_var_var_28.keys()):
                _var_var_47 = _var_var_28[_var_var_38]
                if not _var_var_47:
                    continue
                _var_var_48 = _var_var_29.get(_var_var_38, [])
                _var_var_49 = _var_var_30.get(_var_var_38, [])
                _var_var_50 = _var_var_31.get(_var_var_38, [])
                _var_var_51 = _var_var_32.get(_var_var_38, [])
                _var_var_52 = _var_var_33.get(_var_var_38, [])
                _var_var_53 = (
                    sum((1 for _var_var_67 in _var_var_51 if _var_var_67 > 100))
                    if _var_var_51
                    else 0
                )
                _var_var_54 = (
                    min(100.0, round(sum(_var_var_48) / len(_var_var_48), 1))
                    if _var_var_48
                    else 0
                )
                _var_var_55 = (
                    round(sum(_var_var_49) / len(_var_var_49), 1) if _var_var_49 else 0
                )
                _var_var_56 = round(max(_var_var_49), 1) if _var_var_49 else 0
                _var_var_57 = (
                    max(0.0, round(sum(_var_var_51) / len(_var_var_51), 1))
                    if _var_var_51
                    else 0
                )
                _var_var_58 = (
                    min(100.0, round(sum(_var_var_52) / len(_var_var_52), 1))
                    if _var_var_52
                    else 0
                )
                _var_var_59 = len(_var_var_34.get(_var_var_38, set()))
                _var_var_60 = (
                    _var_var_59 >= 23 and max(_var_var_34.get(_var_var_38, {0})) >= 23
                )
                _var_var_61, _var_var_62, _var_var_63, _var_var_64 = (
                    _aggregate_daily_precipitation(_var_var_50, _var_var_60)
                )
                _var_var_65 = _var_var_61 if _var_var_62 == "amount" else None
                _var_var_66 = _var_var_61 if _var_var_62 == "rate" else None
                _var_var_46.append(
                    {
                        "date": _var_var_38,
                        "hours_count": _var_var_59,
                        "is_complete": _var_var_60,
                        "temp_avg": round(sum(_var_var_47) / len(_var_var_47), 1),
                        "temp_max": round(max(_var_var_47), 1),
                        "temp_min": round(min(_var_var_47), 1),
                        "humidity": _var_var_54,
                        "wind": _var_var_55,
                        "radiation": _var_var_57,
                        "rain": _var_var_61,
                        "precipitation_semantics": _var_var_62,
                        "precipitation_unit": _var_var_63,
                        "precipitation_quality": _var_var_64,
                        "clouds": _var_var_58,
                        "humidity_avg": _var_var_54,
                        "wind_avg": _var_var_55,
                        "wind_max": _var_var_56,
                        "radiation_avg": _var_var_57,
                        "rain_total": _var_var_65,
                        "rain_rate_max": _var_var_66,
                        "sun_hours": _var_var_53,
                        "solar_kwh": _var_var_39.get(_var_var_38, 0),
                    }
                )
                continue
            _LOGGER.info(
                "Loaded %d days of weather history from Solar Forecast ML database",
                len(_var_var_46),
            )
            return _var_var_46
        except Exception as _var_var_45:
            _LOGGER.error(
                "Error loading Solar Forecast ML weather data from database: %s",
                _var_var_45,
                exc_info=True,
            )
            return []

    async def _load_ki_forecast_data(self) -> dict[str, dict[str, Any]]:
        """Load KI forecast data from database. @zara"""
        try:
            _var_var_21 = Path(self.hass.config.path())
            _var_var_22 = WeatherDataReader(_var_var_21)
            if not _var_var_22.is_available:
                _LOGGER.debug("Solar Forecast ML database not found")
                return {}
            _var_var_80 = await _var_var_22.async_get_forecast_weather()
            if not _var_var_80:
                _LOGGER.debug("No forecast data in database")
                return {}
            _var_var_81 = defaultdict(
                lambda: {
                    "temps": [],
                    "radiations": [],
                    "clouds": [],
                    "humidities": [],
                    "winds": [],
                    "rains": [],
                }
            )
            for _var_var_25 in _var_var_80:
                _var_var_38 = _var_var_25.date.isoformat()
                _var_var_20 = _var_var_81[_var_var_38]
                if _var_var_25.temperature_c is not None:
                    _var_var_20["temps"].append(_var_var_25.temperature_c)
                if _var_var_25.solar_radiation_wm2 is not None:
                    _var_var_20["radiations"].append(_var_var_25.solar_radiation_wm2)
                if _var_var_25.cloud_cover_percent is not None:
                    _var_var_20["clouds"].append(_var_var_25.cloud_cover_percent)
                if _var_var_25.humidity_percent is not None:
                    _var_var_20["humidities"].append(_var_var_25.humidity_percent)
                if _var_var_25.wind_speed_ms is not None:
                    _var_var_20["winds"].append(_var_var_25.wind_speed_ms)
                if _var_var_25.precipitation_mm is not None:
                    _var_var_20["rains"].append(_var_var_25.precipitation_mm)
            _var_var_82 = {}
            for _var_var_38, _var_var_14 in _var_var_81.items():
                if _var_var_14["temps"]:
                    _var_var_82[_var_var_38] = {
                        "temp_avg": round(
                            sum(_var_var_14["temps"]) / len(_var_var_14["temps"]), 1
                        ),
                        "temp_max": round(max(_var_var_14["temps"]), 1),
                        "temp_min": round(min(_var_var_14["temps"]), 1),
                        "radiation": (
                            round(
                                sum(_var_var_14["radiations"])
                                / len(_var_var_14["radiations"]),
                                1,
                            )
                            if _var_var_14["radiations"]
                            else 0
                        ),
                        "clouds": (
                            round(
                                sum(_var_var_14["clouds"]) / len(_var_var_14["clouds"]),
                                1,
                            )
                            if _var_var_14["clouds"]
                            else 0
                        ),
                        "humidity": (
                            round(
                                sum(_var_var_14["humidities"])
                                / len(_var_var_14["humidities"]),
                                1,
                            )
                            if _var_var_14["humidities"]
                            else 0
                        ),
                        "wind": (
                            round(
                                sum(_var_var_14["winds"]) / len(_var_var_14["winds"]), 1
                            )
                            if _var_var_14["winds"]
                            else 0
                        ),
                        "rain": (
                            round(sum(_var_var_14["rains"]), 1)
                            if _var_var_14["rains"]
                            else 0
                        ),
                    }
            _LOGGER.info(
                "Loaded KI forecast data for %d days from database", len(_var_var_82)
            )
            return _var_var_82
        except Exception as _var_var_45:
            _LOGGER.error(
                "Error loading KI forecast data from database: %s",
                _var_var_45,
                exc_info=True,
            )
            return {}
