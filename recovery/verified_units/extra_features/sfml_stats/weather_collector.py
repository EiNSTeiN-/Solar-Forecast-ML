# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Weather Data Collector for SFML Stats. @zara"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Any
from collections import defaultdict
from homeassistant.core import HomeAssistant
from .readers.solar_reader import SolarDataReader
from .readers.weather_reader import WeatherDataReader

_LOGGER = logging.getLogger(__name__)


def _hourly_precipitation_mm(
    value: float | None, semantics: str | None, unit: str | None, quality: str | None
) -> float | None:
    """Convert one classified hourly observation into millimetres."""
    if value is None:
        return
    precipitation_mm = max(0.0, float(value))
    if (semantics, quality) in {
        ("counter", "counter_delta"),
        ("daily_total", "daily_total"),
    }:
        return precipitation_mm
    if (
        semantics == "amount"
        and unit == "mm"
        and (quality in {"amount", "direct_amount", "rate_integrated"})
    ):
        return precipitation_mm
    if semantics == "rate" and unit == "mm/h" and (quality == "rate_only"):
        return round(precipitation_mm, 2)
    return None if True else None


def _is_explicit_amount_precipitation(
    samples: list[tuple[float | None, str | None, str | None, str | None]],
) -> bool:
    """True when every numeric sample converts to an hourly millimetre amount."""
    hourly_amounts_mm = [
        _hourly_precipitation_mm(sample_value, semantics, unit, quality)
        for sample_value, semantics, unit, quality in samples
    ]
    has_numeric_sample = any(
        (
            sample_value is not None
            for sample_value, unused_metadata, unused_metadata, unused_metadata in samples
        )
    )
    return (
        has_numeric_sample
        and all(
            (
                sample_value is None
                and hourly_amount_mm is None
                or hourly_amount_mm is not None
                for ((sample_value, unused_metadata, unused_metadata, unused_metadata), hourly_amount_mm) in zip(samples, hourly_amounts_mm)  # fmt: skip
            )
        )
        and any(
            (hourly_amount_mm is not None for hourly_amount_mm in hourly_amounts_mm)
        )
    )


def _is_explicit_rate_precipitation(
    samples: list[tuple[float | None, str | None, str | None, str | None]],
) -> bool:
    """True when numeric samples are classified rain rates."""
    return (
        bool(samples)
        and any(
            (
                sample_value is not None
                for sample_value, unused_metadata, unused_metadata, unused_metadata in samples
            )
        )
        and all(
            (
                sample_value is None
                or (semantics == "rate" and unit == "mm/h" and (quality == "rate_only"))
                for sample_value, semantics, unit, quality in samples
            )
        )
    )


def _aggregate_daily_precipitation(
    samples: list[tuple[float | None, str | None, str | None, str | None]],
    is_complete: bool,
) -> tuple[float | None, str | None, str | None, str]:
    """Return a measured daily precipitation amount in millimetres."""
    completeness_quality = "complete" if is_complete else "partial"
    hourly_amounts_mm = [
        _hourly_precipitation_mm(sample_value, semantics, unit, sample_quality)
        for sample_value, semantics, unit, sample_quality in samples
    ]
    if not (
        _is_explicit_amount_precipitation(samples)
        or _is_explicit_rate_precipitation(samples)
    ):
        return (None, None, None, "unavailable")
    valid_amounts_mm = [
        hourly_amount_mm
        for hourly_amount_mm in hourly_amounts_mm
        if hourly_amount_mm is not None
    ]
    if not valid_amounts_mm:
        return (None, None, None, "unavailable")
    precipitation_mm = max(0.0, round(sum(valid_amounts_mm), 1))
    return (precipitation_mm, "amount", "mm", completeness_quality)


def _classify_rain_unit(
    unit: str | None, state_class: str | None
) -> tuple[str | None, str | None, str | None]:
    normalized_unit = str(unit or "").strip().lower().replace(" ", "")
    if normalized_unit in {"mmh", "mm/h", "mm/hr"}:
        return ("rate", "mm/h", "rate_only")
    if normalized_unit == "mm":
        if str(state_class or "").strip().lower() in {"total", "total_increasing"}:
            return (None, None, None)
        return ("amount", "mm", "direct_amount")
    return (None, None, None)


def _configured_rain_semantics(hass):
    """Classify unlabeled SFML rain rows from the currently configured rain sensor."""
    if hass is None:
        return (None, None, None)
    try:
        config_entries = hass.config_entries.async_entries("solar_forecast_ml")
    except Exception:
        return (None, None, None)
    for entry in config_entries or []:
        entry_data = getattr(entry, "data", None) or {}
        entry_options = getattr(entry, "options", None) or {}
        rain_entity = entry_data.get("rain_sensor") or entry_options.get("rain_sensor")
        if not rain_entity:
            continue
        try:
            rain_state = hass.states.get(rain_entity)
        except Exception:
            continue
        if rain_state is None:
            continue
        rain_attributes = getattr(rain_state, "attributes", None) or {}
        semantics, unit, quality = _classify_rain_unit(
            rain_attributes.get("unit_of_measurement"),
            rain_attributes.get("state_class"),
        )
        if not semantics == "rate":
            continue
        return (semantics, unit, quality)
    return (None, None, None)


class WeatherDataCollector:
    """Collects weather data from Solar Forecast ML. @zara"""

    def __init__(self, hass: HomeAssistant, data_path: Path) -> None:
        """Initialize collector. @zara"""
        self.hass = hass
        self.data_path = data_path
        self.data_path.mkdir(parents=True, exist_ok=True)

    async def collect_daily_data(self) -> None:
        """Weather data is provided by Solar Forecast ML. @zara"""
        _LOGGER.debug("Weather data is provided by Solar Forecast ML")

    async def get_history(
        self, days: int = 30, include_partial: bool = False
    ) -> list[dict[str, Any]]:
        """Get last N days of weather history from Solar Forecast ML. @zara"""
        history = await self._load_from_solar_forecast_ml()
        if not history:
            _LOGGER.warning(
                "No weather data available from Solar Forecast ML. Make sure Solar Forecast ML integration is installed and configured."
            )
            return []
        if not include_partial:
            history = [
                day_record for day_record in history if day_record.get("is_complete")
            ]
        return history[-days:] if len(history) > days else history

    async def _load_from_solar_forecast_ml(self) -> list[dict[str, Any]]:
        """Load and convert weather data from Solar Forecast ML database. @zara"""
        try:
            config_path = Path(self.hass.config.path())
            weather_reader = WeatherDataReader(config_path)
            if not weather_reader.is_available:
                _LOGGER.debug("Solar Forecast ML database not found")
                return []
            hourly_weather = await weather_reader.async_get_hourly_weather()
            archived_weather = await weather_reader.async_get_archived_hourly_weather()
            if archived_weather:
                weather_by_date_hour = {
                    (
                        weather_observation.date,
                        weather_observation.hour,
                    ): weather_observation
                    for weather_observation in archived_weather
                }
                weather_by_date_hour.update(
                    {
                        (
                            weather_observation.date,
                            weather_observation.hour,
                        ): weather_observation
                        for weather_observation in hourly_weather
                    }
                )
                hourly_weather = [
                    weather_by_date_hour[date_hour_key]
                    for date_hour_key in sorted(weather_by_date_hour)
                ]
            if not hourly_weather:
                _LOGGER.debug("No hourly weather data in database")
                return []
            temperatures_by_date = defaultdict(list)
            humidities_by_date = defaultdict(list)
            wind_speeds_by_date = defaultdict(list)
            precipitation_samples_by_date = defaultdict(list)
            radiation_by_date = defaultdict(list)
            cloud_cover_by_date = defaultdict(list)
            hours_by_date = defaultdict(set)
            configured_rain_semantics, configured_rain_unit, configured_rain_quality = (
                _configured_rain_semantics(self.hass)
            )
            for weather_observation in hourly_weather:
                date_key = weather_observation.date.isoformat()
                hours_by_date[date_key].add(weather_observation.hour)
                if weather_observation.temperature_c is not None:
                    temperatures_by_date[date_key].append(
                        weather_observation.temperature_c
                    )
                if weather_observation.humidity_percent is not None:
                    humidities_by_date[date_key].append(
                        weather_observation.humidity_percent
                    )
                if weather_observation.wind_speed_ms is not None:
                    wind_speeds_by_date[date_key].append(
                        weather_observation.wind_speed_ms
                    )
                semantics = weather_observation.precipitation_semantics
                unit = weather_observation.precipitation_unit
                quality = weather_observation.precipitation_quality
                if configured_rain_semantics and (not any((semantics, unit, quality))):
                    semantics, unit, quality = (
                        configured_rain_semantics,
                        configured_rain_unit,
                        configured_rain_quality,
                    )
                if weather_observation.precipitation_mm is not None or any(
                    (semantics, unit, quality)
                ):
                    precipitation_samples_by_date[date_key].append(
                        (weather_observation.precipitation_mm, semantics, unit, quality)
                    )
                if weather_observation.solar_radiation_wm2 is not None:
                    radiation_by_date[date_key].append(
                        weather_observation.solar_radiation_wm2
                    )
                if weather_observation.cloud_cover_percent is not None:
                    cloud_cover_by_date[date_key].append(
                        weather_observation.cloud_cover_percent
                    )
            solar_yield_by_date = {}
            try:
                config_path = Path(self.hass.config.path())
                solar_reader = SolarDataReader(config_path, hass=self.hass)
                solar_summaries = await solar_reader.async_get_daily_summaries()
                for solar_summary in solar_summaries:
                    solar_date_key = solar_summary.date.isoformat()
                    actual_solar_kwh = solar_summary.actual_total_kwh
                    if solar_date_key and actual_solar_kwh:
                        solar_yield_by_date[solar_date_key] = actual_solar_kwh
            except Exception as error:
                _LOGGER.warning(
                    "Could not load solar summaries from database: %s", error
                )
            daily_weather = []
            for date_key in sorted(temperatures_by_date.keys()):
                temperatures_c = temperatures_by_date[date_key]
                if not temperatures_c:
                    continue
                humidity_samples = humidities_by_date.get(date_key, [])
                wind_samples_ms = wind_speeds_by_date.get(date_key, [])
                precipitation_samples = precipitation_samples_by_date.get(date_key, [])
                radiation_samples_wm2 = radiation_by_date.get(date_key, [])
                cloud_cover_samples = cloud_cover_by_date.get(date_key, [])
                sun_hours = (
                    sum(
                        (
                            1
                            for radiation_wm2 in radiation_samples_wm2
                            if radiation_wm2 > 100
                        )
                    )
                    if radiation_samples_wm2
                    else 0
                )
                mean_humidity_percent = (
                    min(100.0, round(sum(humidity_samples) / len(humidity_samples), 1))
                    if humidity_samples
                    else 0
                )
                mean_wind_ms = (
                    round(sum(wind_samples_ms) / len(wind_samples_ms), 1)
                    if wind_samples_ms
                    else 0
                )
                max_wind_ms = round(max(wind_samples_ms), 1) if wind_samples_ms else 0
                mean_radiation_wm2 = (
                    max(
                        0.0,
                        round(
                            sum(radiation_samples_wm2) / len(radiation_samples_wm2), 1
                        ),
                    )
                    if radiation_samples_wm2
                    else 0
                )
                mean_cloud_cover_percent = (
                    min(
                        100.0,
                        round(sum(cloud_cover_samples) / len(cloud_cover_samples), 1),
                    )
                    if cloud_cover_samples
                    else 0
                )
                hours_count = len(hours_by_date.get(date_key, set()))
                is_complete = (
                    hours_count >= 23 and max(hours_by_date.get(date_key, {0})) >= 23
                )
                (
                    daily_precipitation,
                    daily_precipitation_semantics,
                    daily_precipitation_unit,
                    daily_precipitation_quality,
                ) = _aggregate_daily_precipitation(precipitation_samples, is_complete)
                rain_total_mm = (
                    daily_precipitation
                    if daily_precipitation_semantics == "amount"
                    else None
                )
                rain_rate_mm_per_hour = (
                    daily_precipitation
                    if daily_precipitation_semantics == "rate"
                    else None
                )
                daily_weather.append(
                    {
                        "date": date_key,
                        "hours_count": hours_count,
                        "is_complete": is_complete,
                        "temp_avg": round(sum(temperatures_c) / len(temperatures_c), 1),
                        "temp_max": round(max(temperatures_c), 1),
                        "temp_min": round(min(temperatures_c), 1),
                        "humidity": mean_humidity_percent,
                        "wind": mean_wind_ms,
                        "radiation": mean_radiation_wm2,
                        "rain": daily_precipitation,
                        "precipitation_semantics": daily_precipitation_semantics,
                        "precipitation_unit": daily_precipitation_unit,
                        "precipitation_quality": daily_precipitation_quality,
                        "clouds": mean_cloud_cover_percent,
                        "humidity_avg": mean_humidity_percent,
                        "wind_avg": mean_wind_ms,
                        "wind_max": max_wind_ms,
                        "radiation_avg": mean_radiation_wm2,
                        "rain_total": rain_total_mm,
                        "rain_rate_max": rain_rate_mm_per_hour,
                        "sun_hours": sun_hours,
                        "solar_kwh": solar_yield_by_date.get(date_key, 0),
                    }
                )
                continue
            _LOGGER.info(
                "Loaded %d days of weather history from Solar Forecast ML database",
                len(daily_weather),
            )
            return daily_weather
        except Exception as error:
            _LOGGER.error(
                "Error loading Solar Forecast ML weather data from database: %s",
                error,
                exc_info=True,
            )
            return []

    async def get_comparison_data(self, days: int = 7) -> dict[str, Any]:
        """Get IST vs KI comparison data for weather analytics. @zara"""
        actual_history = [
            day_record
            for day_record in await self._load_from_solar_forecast_ml()
            if day_record.get("is_complete")
        ]
        forecast_by_date = await self._load_ki_forecast_data()
        if not actual_history:
            _LOGGER.warning("No IST weather data available for comparison")
            return {"success": False, "error": "No IST data available"}
        actual_history = (
            actual_history[-days:] if len(actual_history) > days else actual_history
        )
        comparisons = []
        for actual_day in actual_history:
            date_key = actual_day.get("date")
            forecast_day = forecast_by_date.get(date_key, {})
            comparisons.append(
                {
                    "date": date_key,
                    "temp_ist": actual_day.get("temp_avg", 0),
                    "temp_ist_max": actual_day.get("temp_max", 0),
                    "temp_ist_min": actual_day.get("temp_min", 0),
                    "radiation_ist": actual_day.get("radiation", 0),
                    "clouds_ist": actual_day.get("clouds", 0),
                    "humidity_ist": actual_day.get("humidity", 0),
                    "wind_ist": actual_day.get("wind", 0),
                    "rain_ist": actual_day.get("rain", 0),
                    "temp_ki": forecast_day.get("temp_avg", 0),
                    "temp_ki_max": forecast_day.get("temp_max", 0),
                    "temp_ki_min": forecast_day.get("temp_min", 0),
                    "radiation_ki": forecast_day.get("radiation", 0),
                    "clouds_ki": forecast_day.get("clouds", 0),
                    "humidity_ki": forecast_day.get("humidity", 0),
                    "wind_ki": forecast_day.get("wind", 0),
                    "rain_ki": forecast_day.get("rain", 0),
                }
            )
            continue
        if comparisons:
            temperature_errors_c = [
                abs(comparison["temp_ist"] - comparison["temp_ki"])
                for comparison in comparisons
                if comparison["temp_ki"] != 0
            ]
            radiation_errors_wm2 = [
                abs(comparison["radiation_ist"] - comparison["radiation_ki"])
                for comparison in comparisons
                if comparison["radiation_ki"] != 0
            ]
            mean_temperature_error_c = (
                sum(temperature_errors_c) / len(temperature_errors_c)
                if temperature_errors_c
                else 0
            )
            mean_radiation_error_wm2 = (
                sum(radiation_errors_wm2) / len(radiation_errors_wm2)
                if radiation_errors_wm2
                else 0
            )
            temperature_accuracy = max(0, 100 - mean_temperature_error_c * 10)
            radiation_accuracy = max(0, 100 - mean_radiation_error_wm2 / 5)
        else:
            temperature_accuracy = 0
            radiation_accuracy = 0
        return {
            "success": True,
            "data": comparisons,
            "stats": {
                "temp_accuracy": round(temperature_accuracy, 1),
                "radiation_accuracy": round(radiation_accuracy, 1),
                "overall_accuracy": round(
                    (temperature_accuracy + radiation_accuracy) / 2, 1
                ),
                "days_compared": len(comparisons),
            },
        }

    async def _load_ki_forecast_data(self) -> dict[str, dict[str, Any]]:
        """Load KI forecast data from database. @zara"""
        try:
            config_path = Path(self.hass.config.path())
            weather_reader = WeatherDataReader(config_path)
            if not weather_reader.is_available:
                _LOGGER.debug("Solar Forecast ML database not found")
                return {}
            forecast_weather = await weather_reader.async_get_forecast_weather()
            if not forecast_weather:
                _LOGGER.debug("No forecast data in database")
                return {}
            forecast_samples_by_date = defaultdict(
                lambda: {
                    "temps": [],
                    "radiations": [],
                    "clouds": [],
                    "humidities": [],
                    "winds": [],
                    "rains": [],
                }
            )
            for weather_observation in forecast_weather:
                date_key = weather_observation.date.isoformat()
                forecast_day_samples = forecast_samples_by_date[date_key]
                if weather_observation.temperature_c is not None:
                    forecast_day_samples["temps"].append(
                        weather_observation.temperature_c
                    )
                if weather_observation.solar_radiation_wm2 is not None:
                    forecast_day_samples["radiations"].append(
                        weather_observation.solar_radiation_wm2
                    )
                if weather_observation.cloud_cover_percent is not None:
                    forecast_day_samples["clouds"].append(
                        weather_observation.cloud_cover_percent
                    )
                if weather_observation.humidity_percent is not None:
                    forecast_day_samples["humidities"].append(
                        weather_observation.humidity_percent
                    )
                if weather_observation.wind_speed_ms is not None:
                    forecast_day_samples["winds"].append(
                        weather_observation.wind_speed_ms
                    )
                if weather_observation.precipitation_mm is not None:
                    forecast_day_samples["rains"].append(
                        weather_observation.precipitation_mm
                    )
            forecast_by_date = {}
            for date_key, forecast_samples in forecast_samples_by_date.items():
                if forecast_samples["temps"]:
                    forecast_by_date[date_key] = {
                        "temp_avg": round(
                            sum(forecast_samples["temps"])
                            / len(forecast_samples["temps"]),
                            1,
                        ),
                        "temp_max": round(max(forecast_samples["temps"]), 1),
                        "temp_min": round(min(forecast_samples["temps"]), 1),
                        "radiation": (
                            round(
                                sum(forecast_samples["radiations"])
                                / len(forecast_samples["radiations"]),
                                1,
                            )
                            if forecast_samples["radiations"]
                            else 0
                        ),
                        "clouds": (
                            round(
                                sum(forecast_samples["clouds"])
                                / len(forecast_samples["clouds"]),
                                1,
                            )
                            if forecast_samples["clouds"]
                            else 0
                        ),
                        "humidity": (
                            round(
                                sum(forecast_samples["humidities"])
                                / len(forecast_samples["humidities"]),
                                1,
                            )
                            if forecast_samples["humidities"]
                            else 0
                        ),
                        "wind": (
                            round(
                                sum(forecast_samples["winds"])
                                / len(forecast_samples["winds"]),
                                1,
                            )
                            if forecast_samples["winds"]
                            else 0
                        ),
                        "rain": (
                            round(sum(forecast_samples["rains"]), 1)
                            if forecast_samples["rains"]
                            else 0
                        ),
                    }
            _LOGGER.info(
                "Loaded KI forecast data for %d days from database",
                len(forecast_by_date),
            )
            return forecast_by_date
        except Exception as error:
            _LOGGER.error(
                "Error loading KI forecast data from database: %s", error, exc_info=True
            )
            return {}

    async def get_statistics(self) -> dict[str, Any]:
        """Calculate weather statistics from Solar Forecast ML history. @zara"""
        history = await self.get_history(days=365)
        if not history:
            return {
                "avgTemp": 0,
                "maxTemp": 0,
                "minTemp": 0,
                "totalRain": None,
                "maxRainRate": None,
                "avgWind": 0,
                "sunHours": 0,
            }
        recent_week = history[-7:] if len(history) >= 7 else history
        recent_month = history[-30:] if len(history) >= 30 else history
        sun_hours = sum((day_record.get("sun_hours", 0) for day_record in recent_week))
        return (
            {
                "avgTemp": (
                    round(
                        sum(
                            (
                                day_record.get("temp_avg", 0)
                                for day_record in recent_week
                            )
                        )
                        / len(recent_week),
                        1,
                    )
                    if recent_week
                    else 0
                ),
                "maxTemp": round(
                    max(
                        (day_record.get("temp_max", 0) for day_record in history),
                        default=0,
                    ),
                    1,
                ),
                "minTemp": round(
                    min(
                        (day_record.get("temp_min", 0) for day_record in history),
                        default=0,
                    ),
                    1,
                ),
                "totalRain": (
                    round(
                        sum(
                            (
                                day_record["rain_total"]
                                for day_record in recent_month
                                if day_record.get("precipitation_semantics") == "amount"
                                if day_record.get("rain_total") is not None
                            )
                        ),
                        1,
                    )
                    if any(
                        (
                            day_record.get("precipitation_semantics") == "amount"
                            and day_record.get("rain_total") is not None
                            for day_record in recent_month
                        )
                    )
                    else None
                ),
                "maxRainRate": (
                    round(
                        max(
                            (
                                day_record["rain_rate_max"]
                                for day_record in recent_month
                                if day_record.get("precipitation_semantics") == "rate"
                                if day_record.get("rain_rate_max") is not None
                            )
                        ),
                        1,
                    )
                    if any(
                        (
                            day_record.get("precipitation_semantics") == "rate"
                            and day_record.get("rain_rate_max") is not None
                            for day_record in recent_month
                        )
                    )
                    else None
                ),
                "avgWind": (
                    round(
                        sum((day_record.get("wind_avg", 0) for day_record in history))
                        / len(history),
                        1,
                    )
                    if history
                    else 0
                ),
                "sunHours": sun_hours,
            }
            if True
            else None
        )
