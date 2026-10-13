# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Weather Precision Tracker V16.2.0 - Track Weather Forecast Accuracy

Tracks weather forecast accuracy against local sensors and calculates
correction factors for improving forecast precision.

Compares RAW Open-Meteo forecast vs Actual sensor readings and calculates:
- Temperature offset (additive)
- Solar radiation factor (multiplicative)
- Cloud cover factor (multiplicative)
- Humidity, wind, rain factors (multiplicative)
- Pressure offset (additive)

Database Table: weather_precision_factors
@zara
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from .data_io import DataManagerIO

_LOGGER = logging.getLogger(__name__)
ROLLING_WINDOW_DAYS = 7
MIN_HOURS_FOR_FACTOR = 4
SOLAR_FACTOR_MIN = 0.5
SOLAR_FACTOR_MAX = 2.0


class WeatherPrecisionTracker(DataManagerIO):
    """Tracks weather forecast accuracy against local sensors. @zara

    Calculates correction factors by comparing RAW forecasts vs actual measurements.
    All data stored in database via DatabaseManager.
    """

    def __init__(
        self, hass: HomeAssistant, db_manager: DatabaseManager, config_entry=None
    ):
        """Initialize the weather precision tracker. @zara

        Args:
            hass: Home Assistant instance
            db_manager: DatabaseManager for data persistence
            config_entry: Config entry with sensor mappings
        """
        super().__init__(hass, db_manager)
        self.config_entry = config_entry
        self.sensors = {}
        if config_entry:
            from ..const import (
                CONF_TEMP_SENSOR as temperature_sensor_key,
                CONF_HUMIDITY_SENSOR as humidity_sensor_key,
                CONF_WIND_SENSOR as wind_sensor_key,
                CONF_RAIN_SENSOR as rain_sensor_key,
                CONF_PRESSURE_SENSOR as pressure_sensor_key,
            )

            self.sensors = {
                "temp": config_entry.data.get(temperature_sensor_key),
                "humidity": config_entry.data.get(humidity_sensor_key),
                "wind": config_entry.data.get(wind_sensor_key),
                "rain": config_entry.data.get(rain_sensor_key),
                "pressure": config_entry.data.get(pressure_sensor_key),
            }
        configured_fields = [
            sensor_field
            for sensor_field, sensor_entity_id in self.sensors.items()
            if sensor_entity_id
        ]
        _LOGGER.info(
            "WeatherPrecisionTracker initialized with sensors: %s", configured_fields
        )

    async def async_init(self) -> bool:
        """Initialize and ensure table exists. @zara"""
        await self.ensure_initialized()
        await self._ensure_tables()
        return True

    async def _ensure_tables(self) -> None:
        """Ensure precision tables exist. @zara"""
        try:
            await self.execute_query(
                "CREATE TABLE IF NOT EXISTS weather_precision_factors (\n                       id INTEGER PRIMARY KEY CHECK (id = 1),\n                       temperature_factor REAL DEFAULT 0.0,\n                       solar_factor REAL DEFAULT 1.0,\n                       cloud_factor REAL DEFAULT 1.0,\n                       wind_factor REAL DEFAULT 1.0,\n                       humidity_factor REAL DEFAULT 1.0,\n                       rain_factor REAL DEFAULT 1.0,\n                       pressure_factor REAL DEFAULT 0.0,\n                       sample_days INTEGER DEFAULT 0,\n                       confidence REAL DEFAULT 0.0,\n                       updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                   )"
            )
            await self.execute_query(
                "CREATE TABLE IF NOT EXISTS weather_precision_daily (\n                       id INTEGER PRIMARY KEY AUTOINCREMENT,\n                       date DATE NOT NULL,\n                       hour INTEGER NOT NULL,\n                       temp_forecast REAL,\n                       actual_temp REAL,\n                       solar_forecast REAL,\n                       solar_actual REAL,\n                       forecast_cloud REAL,\n                       actual_cloud REAL,\n                       humidity_forecast REAL,\n                       humidity_actual REAL,\n                       wind_forecast REAL,\n                       wind_actual REAL,\n                       rain_forecast REAL,\n                       rain_actual REAL,\n                       pressure_forecast REAL,\n                       pressure_actual REAL,\n                       UNIQUE(date, hour)\n                   )"
            )
        except Exception as error:
            _LOGGER.error("Error creating precision tables: %s", error)
        return None if True else None

    async def calculate_correction_factors_v2(
        self, weather_actual_tracker, date_str: Optional[str] = None
    ) -> bool:
        """Calculate correction factors for all weather fields. @zara

        Compares RAW Open-Meteo forecast vs actual sensor readings.

        Args:
            weather_actual_tracker: WeatherActualTracker instance
            date_str: Date to calculate (default: today)

        Returns:
            True if calculation was successful
        """
        try:
            if date_str is None:
                date_str = datetime.now().strftime("%Y-%m-%d")
            _LOGGER.info("Calculating correction factors for %s", date_str)
            actual_by_hour = await weather_actual_tracker.get_daily_actual_weather(
                date_str
            )
            if not actual_by_hour:
                _LOGGER.info("No actual weather data for %s", date_str)
                return False
            _LOGGER.info("Loaded actual weather for %d hours", len(actual_by_hour))
            forecast_by_hour = await self._load_raw_forecasts(date_str)
            if not forecast_by_hour:
                _LOGGER.debug("No raw forecast data for %s", date_str)
                return False
            _LOGGER.info("Loaded raw forecasts for %d hours", len(forecast_by_hour))
            hourly_comparisons = []
            for hour, actual_weather in actual_by_hour.items():
                if hour not in forecast_by_hour:
                    continue
                forecast_weather = forecast_by_hour[hour]
                comparison = self._compare_weather_fields(
                    forecast_weather, actual_weather, hour
                )
                if comparison:
                    hourly_comparisons.append(comparison)
                    await self._save_hourly_comparison(
                        date_str, hour, forecast_weather, actual_weather
                    )
            if not hourly_comparisons:
                _LOGGER.debug("No valid hour comparisons found")
                return False
            _LOGGER.info("Compared %d hours successfully", len(hourly_comparisons))
            daily_factors = self._calculate_daily_factors(hourly_comparisons)
            await self._save_daily_factors(
                date_str, daily_factors, len(hourly_comparisons)
            )
            await self._update_rolling_averages()
            _LOGGER.info(
                "Correction factors calculated for %s: %d hours, factors: %s",
                date_str,
                len(hourly_comparisons),
                daily_factors,
            )
        except Exception as error:
            _LOGGER.error(
                "Error calculating correction factors: %s", error, exc_info=True
            )
            return False
        return True

    async def _load_raw_forecasts(self, date_str: str) -> dict[int, dict[str, Any]]:
        """Load raw forecast data from database. @zara"""
        try:
            rows = await self.fetch_all(
                "SELECT hour, temperature, humidity, cloud_cover, precipitation, wind_speed,\n                          pressure, ghi\n                   FROM weather_cache_open_meteo\n                   WHERE forecast_date = ?",
                (date_str,),
            )
            result = {}
            for row in rows:
                result[row[0]] = {
                    "temperature": row[1],
                    "humidity": row[2],
                    "cloud_cover": row[3],
                    "precipitation": row[4],
                    "wind_speed": row[5],
                    "pressure": row[6],
                    "ghi": row[7],
                }
            return result
        except Exception as error:
            _LOGGER.error("Error loading raw forecasts: %s", error)
            return {}

    def _compare_weather_fields(
        self, forecast: dict[str, Any], actual: dict[str, Any], hour: int
    ) -> Optional[dict[str, Any]]:
        """Compare forecast vs actual for a single hour. @zara"""
        comparison = {"hour": hour, "fields": {}}
        compared_field_count = 0
        forecast_temperature = forecast.get("temperature")
        actual_temperature = actual.get("temperature_c")
        if forecast_temperature is not None and actual_temperature is not None:
            temperature_offset = actual_temperature - forecast_temperature
            comparison["fields"]["temperature"] = {
                "forecast": forecast_temperature,
                "actual": actual_temperature,
                "offset": round(temperature_offset, 2),
            }
            compared_field_count += 1
        forecast_solar_radiation = forecast.get("ghi")
        actual_solar_radiation = actual.get("solar_radiation_wm2")
        if (
            forecast_solar_radiation is not None
            and actual_solar_radiation is not None
            and (forecast_solar_radiation > 0)
        ):
            factor = actual_solar_radiation / forecast_solar_radiation
            comparison["fields"]["solar_radiation_wm2"] = {
                "forecast": forecast_solar_radiation,
                "actual": actual_solar_radiation,
                "factor": round(factor, 3),
            }
            compared_field_count += 1
        forecast_cloud_cover = forecast.get("cloud_cover")
        actual_cloud_cover = actual.get("cloud_cover_percent")
        if (
            forecast_cloud_cover is not None
            and actual_cloud_cover is not None
            and (forecast_cloud_cover > 0)
        ):
            factor = actual_cloud_cover / forecast_cloud_cover
            comparison["fields"]["clouds"] = {
                "forecast": forecast_cloud_cover,
                "actual": actual_cloud_cover,
                "factor": round(factor, 3),
            }
            compared_field_count += 1
        forecast_humidity = forecast.get("humidity")
        actual_humidity = actual.get("humidity_percent")
        if (
            forecast_humidity is not None
            and actual_humidity is not None
            and (forecast_humidity > 0)
        ):
            factor = actual_humidity / forecast_humidity
            comparison["fields"]["humidity"] = {
                "forecast": forecast_humidity,
                "actual": actual_humidity,
                "factor": round(factor, 3),
            }
            compared_field_count += 1
        forecast_wind = forecast.get("wind_speed")
        actual_wind = actual.get("wind_speed_ms")
        if (
            forecast_wind is not None
            and actual_wind is not None
            and (forecast_wind > 0)
        ):
            factor = actual_wind / forecast_wind
            comparison["fields"]["wind"] = {
                "forecast": forecast_wind,
                "actual": actual_wind,
                "factor": round(factor, 3),
            }
            compared_field_count += 1
        forecast_rain = forecast.get("precipitation") or 0
        actual_rain = actual.get("precipitation_mm") or 0
        if forecast_rain is not None and actual_rain is not None:
            rain_difference = actual_rain - forecast_rain
            comparison["fields"]["rain"] = {
                "forecast": forecast_rain,
                "actual": actual_rain,
                "difference": round(rain_difference, 2),
            }
            compared_field_count += 1
        forecast_pressure = forecast.get("pressure")
        actual_pressure = actual.get("pressure_hpa")
        if forecast_pressure is not None and actual_pressure is not None:
            temperature_offset = actual_pressure - forecast_pressure
            comparison["fields"]["pressure"] = {
                "forecast": forecast_pressure,
                "actual": actual_pressure,
                "offset": round(temperature_offset, 2),
            }
            compared_field_count += 1
        return comparison if compared_field_count > 0 else None

    def _calculate_daily_factors(
        self, hourly_comparisons: list[dict[str, Any]]
    ) -> dict[str, float]:
        """Calculate daily average correction factors. @zara"""
        offset_values = {"temperature": [], "pressure": []}
        factor_values = {
            "solar_radiation_wm2": [],
            "clouds": [],
            "humidity": [],
            "wind": [],
        }
        difference_values = {"rain": []}
        for hourly_comparison in hourly_comparisons:
            comparison_fields = hourly_comparison.get("fields", {})
            for field, field_comparison in comparison_fields.items():
                if field in offset_values and "offset" in field_comparison:
                    temperature_offset = field_comparison["offset"]
                    if -20.0 <= temperature_offset <= 20.0:
                        offset_values[field].append(temperature_offset)
                elif field in factor_values and "factor" in field_comparison:
                    factor = field_comparison["factor"]
                    if SOLAR_FACTOR_MIN <= factor <= SOLAR_FACTOR_MAX:
                        factor_values[field].append(factor)
                elif field in difference_values and "difference" in field_comparison:
                    rain_difference = field_comparison["difference"]
                    if -50.0 <= rain_difference <= 50.0:
                        difference_values[field].append(rain_difference)
            continue
        result = {}
        for offset_field, values in offset_values.items():
            if values:
                result[f"{offset_field}_offset"] = round(sum(values) / len(values), 2)
            else:
                result[f"{offset_field}_offset"] = 0.0
        for offset_field, values in factor_values.items():
            if values:
                result[f"{offset_field}_factor"] = round(sum(values) / len(values), 3)
            else:
                result[f"{offset_field}_factor"] = 1.0
        for offset_field, values in difference_values.items():
            if values:
                result[f"{offset_field}_diff"] = round(sum(values) / len(values), 2)
            else:
                result[f"{offset_field}_diff"] = 0.0
        return result

    async def _save_hourly_comparison(
        self, date_str: str, hour: int, forecast: dict[str, Any], actual: dict[str, Any]
    ) -> None:
        try:
            forecast_temperature = forecast.get("temperature")
            actual_temperature = actual.get("temperature_c")
            temperature_offset = (
                actual_temperature - forecast_temperature
                if forecast_temperature is not None and actual_temperature is not None
                else None
            )
            forecast_humidity = forecast.get("humidity")
            actual_humidity = actual.get("humidity_percent")
            humidity_factor = (
                actual_humidity / forecast_humidity
                if forecast_humidity and actual_humidity
                else None
            )
            forecast_wind = forecast.get("wind_speed")
            actual_wind = actual.get("wind_speed_ms")
            wind_factor = (
                actual_wind / forecast_wind if forecast_wind and actual_wind else None
            )
            forecast_rain = forecast.get("precipitation")
            actual_rain = actual.get("precipitation_mm")
            rain_difference = (
                actual_rain - forecast_rain
                if forecast_rain is not None and actual_rain is not None
                else None
            )
            forecast_pressure = forecast.get("pressure")
            actual_pressure = actual.get("pressure_hpa")
            pressure_offset = (
                actual_pressure - forecast_pressure
                if forecast_pressure is not None and actual_pressure is not None
                else None
            )
            forecast_solar_radiation = forecast.get("ghi")
            actual_solar_radiation = actual.get("solar_radiation_wm2")
            solar_factor = (
                actual_solar_radiation / forecast_solar_radiation
                if forecast_solar_radiation
                and actual_solar_radiation
                and (forecast_solar_radiation > 0)
                else None
            )
            forecast_cloud_cover = forecast.get("cloud_cover")
            actual_cloud_cover = actual.get("cloud_cover_percent")
            cloud_factor = (
                actual_cloud_cover / forecast_cloud_cover
                if forecast_cloud_cover
                and actual_cloud_cover
                and (forecast_cloud_cover > 0)
                else None
            )
            await self.execute_query(
                "INSERT INTO weather_precision_daily\n                   (date, hour, temp_forecast, temp_actual, temp_offset,\n                    humidity_forecast, humidity_actual, humidity_factor,\n                    wind_forecast, wind_actual, wind_factor,\n                    rain_forecast, rain_actual, rain_difference,\n                    pressure_forecast, pressure_actual, pressure_offset,\n                    solar_forecast, solar_actual, solar_factor,\n                    clouds_forecast, clouds_actual, clouds_factor)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(date, hour) DO UPDATE SET\n                       temp_forecast = excluded.temp_forecast,\n                       temp_actual = excluded.temp_actual,\n                       temp_offset = excluded.temp_offset,\n                       humidity_forecast = excluded.humidity_forecast,\n                       humidity_actual = excluded.humidity_actual,\n                       humidity_factor = excluded.humidity_factor,\n                       wind_forecast = excluded.wind_forecast,\n                       wind_actual = excluded.wind_actual,\n                       wind_factor = excluded.wind_factor,\n                       rain_forecast = excluded.rain_forecast,\n                       rain_actual = excluded.rain_actual,\n                       rain_difference = excluded.rain_difference,\n                       pressure_forecast = excluded.pressure_forecast,\n                       pressure_actual = excluded.pressure_actual,\n                       pressure_offset = excluded.pressure_offset,\n                       solar_forecast = excluded.solar_forecast,\n                       solar_actual = excluded.solar_actual,\n                       solar_factor = excluded.solar_factor,\n                       clouds_forecast = excluded.clouds_forecast,\n                       clouds_actual = excluded.clouds_actual,\n                       clouds_factor = excluded.clouds_factor",
                (
                    date_str,
                    hour,
                    forecast_temperature,
                    actual_temperature,
                    temperature_offset,
                    forecast_humidity,
                    actual_humidity,
                    humidity_factor,
                    forecast_wind,
                    actual_wind,
                    wind_factor,
                    forecast_rain,
                    actual_rain,
                    rain_difference,
                    forecast_pressure,
                    actual_pressure,
                    pressure_offset,
                    forecast_solar_radiation,
                    actual_solar_radiation,
                    solar_factor,
                    forecast_cloud_cover,
                    actual_cloud_cover,
                    cloud_factor,
                ),
            )
        except Exception as error:
            _LOGGER.error("Error saving hourly comparison: %s", error)
        return None if True else None

    async def _save_daily_factors(
        self, date_str: str, factors: dict[str, float], hours_tracked: int
    ) -> None:
        """Save daily factors to database. @zara"""
        try:
            await self.execute_query(
                "INSERT INTO weather_precision_daily_summary\n                   (date, hours_tracked, avg_temp_offset, avg_solar_factor,\n                    avg_clouds_factor, avg_wind_factor, avg_humidity_factor,\n                    avg_rain_diff, avg_pressure_offset)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(date) DO UPDATE SET\n                       hours_tracked = excluded.hours_tracked,\n                       avg_temp_offset = excluded.avg_temp_offset,\n                       avg_solar_factor = excluded.avg_solar_factor,\n                       avg_clouds_factor = excluded.avg_clouds_factor,\n                       avg_wind_factor = excluded.avg_wind_factor,\n                       avg_humidity_factor = excluded.avg_humidity_factor,\n                       avg_rain_diff = excluded.avg_rain_diff,\n                       avg_pressure_offset = excluded.avg_pressure_offset",
                (
                    date_str,
                    hours_tracked,
                    factors.get("temperature_offset", 0.0),
                    factors.get("solar_radiation_wm2_factor", 1.0),
                    factors.get("clouds_factor", 1.0),
                    factors.get("wind_factor", 1.0),
                    factors.get("humidity_factor", 1.0),
                    factors.get("rain_diff", 0.0),
                    factors.get("pressure_offset", 0.0),
                ),
            )
        except Exception as error:
            _LOGGER.error("Error saving daily factors: %s", error)
        return None if True else None

    async def _update_rolling_averages(self) -> None:
        """Update rolling average correction factors with hourly + weather-specific. @zara

        V16.1 enhanced: Now calculates:
        - Daily rolling averages (as before)
        - Hourly factors for solar_radiation_wm2 and clouds (hours 7-17)
        - Weather-specific factors for clear/cloudy days
        """
        import math as math_module

        try:
            clear_cloud_threshold = 30
            cloudy_cloud_threshold = 50
            min_hourly_samples = 3
            min_weather_samples = 2
            cutoff_date = (
                datetime.now() - timedelta(days=ROLLING_WINDOW_DAYS)
            ).strftime("%Y-%m-%d")
            neutral_factor_days = await self.fetch_all(
                "SELECT ds.date\n                   FROM weather_precision_daily_summary ds\n                   WHERE ds.date >= ?\n                     AND ds.avg_solar_factor = 1.0\n                     AND ds.avg_temp_offset = 0.0",
                (cutoff_date,),
            )
            if neutral_factor_days:
                for day_row in neutral_factor_days:
                    date_str = str(day_row[0])
                    comparison_rows = await self.fetch_all(
                        "SELECT solar_factor, temp_offset, clouds_factor,\n                                  wind_factor, humidity_factor, rain_difference,\n                                  pressure_offset\n                           FROM weather_precision_daily\n                           WHERE date = ? AND solar_factor IS NOT NULL",
                        (date_str,),
                    )
                    if comparison_rows:
                        valid_solar_factors = [
                            comparison_row[0]
                            for comparison_row in comparison_rows
                            if comparison_row[0] is not None
                            if SOLAR_FACTOR_MIN <= comparison_row[0] <= SOLAR_FACTOR_MAX
                        ]
                        valid_temperature_offsets = [
                            comparison_row[1]
                            for comparison_row in comparison_rows
                            if comparison_row[1] is not None
                            if -20 <= comparison_row[1] <= 20
                        ]
                        valid_cloud_factors = [
                            comparison_row[2]
                            for comparison_row in comparison_rows
                            if comparison_row[2] is not None
                            if 0.1 <= comparison_row[2] <= 10.0
                        ]
                        valid_wind_factors = [
                            comparison_row[3]
                            for comparison_row in comparison_rows
                            if comparison_row[3] is not None
                            if 0.1 <= comparison_row[3] <= 10.0
                        ]
                        valid_humidity_factors = [
                            comparison_row[4]
                            for comparison_row in comparison_rows
                            if comparison_row[4] is not None
                            if 0.1 <= comparison_row[4] <= 10.0
                        ]
                        rain_differences = [
                            comparison_row[5]
                            for comparison_row in comparison_rows
                            if comparison_row[5] is not None
                        ]
                        pressure_offsets = [
                            comparison_row[6]
                            for comparison_row in comparison_rows
                            if comparison_row[6] is not None
                        ]
                        await self.execute_query(
                            "UPDATE weather_precision_daily_summary\n                               SET avg_solar_factor = ?,\n                                   avg_temp_offset = ?,\n                                   avg_clouds_factor = ?,\n                                   avg_wind_factor = ?,\n                                   avg_humidity_factor = ?,\n                                   avg_rain_diff = ?,\n                                   avg_pressure_offset = ?\n                               WHERE date = ?",
                            (
                                (
                                    round(
                                        sum(valid_solar_factors)
                                        / len(valid_solar_factors),
                                        3,
                                    )
                                    if valid_solar_factors
                                    else 1.0
                                ),
                                (
                                    round(
                                        sum(valid_temperature_offsets)
                                        / len(valid_temperature_offsets),
                                        2,
                                    )
                                    if valid_temperature_offsets
                                    else 0.0
                                ),
                                (
                                    round(
                                        sum(valid_cloud_factors)
                                        / len(valid_cloud_factors),
                                        3,
                                    )
                                    if valid_cloud_factors
                                    else 1.0
                                ),
                                (
                                    round(
                                        sum(valid_wind_factors)
                                        / len(valid_wind_factors),
                                        3,
                                    )
                                    if valid_wind_factors
                                    else 1.0
                                ),
                                (
                                    round(
                                        sum(valid_humidity_factors)
                                        / len(valid_humidity_factors),
                                        3,
                                    )
                                    if valid_humidity_factors
                                    else 1.0
                                ),
                                (
                                    round(
                                        sum(rain_differences) / len(rain_differences), 2
                                    )
                                    if rain_differences
                                    else 0.0
                                ),
                                (
                                    round(
                                        sum(pressure_offsets) / len(pressure_offsets), 2
                                    )
                                    if pressure_offsets
                                    else 0.0
                                ),
                                date_str,
                            ),
                        )
                        continue
                _LOGGER.info(
                    "Repaired %d daily_summary rows from hourly data",
                    len(neutral_factor_days),
                )
            hourly_rows = await self.fetch_all(
                "SELECT date, hour, solar_forecast, solar_actual, solar_factor,\n                          clouds_forecast, clouds_actual, clouds_factor,\n                          temp_forecast, temp_actual, temp_offset,\n                          humidity_forecast, humidity_actual, humidity_factor,\n                          wind_forecast, wind_actual, wind_factor,\n                          rain_forecast, rain_actual, rain_difference,\n                          pressure_forecast, pressure_actual, pressure_offset\n                   FROM weather_precision_daily\n                   WHERE date >= ?\n                   ORDER BY date, hour",
                (cutoff_date,),
            )
            daily_rows = await self.fetch_all(
                "SELECT date, hours_tracked, avg_temp_offset, avg_solar_factor,\n                          avg_clouds_factor, avg_wind_factor, avg_humidity_factor,\n                          avg_rain_diff, avg_pressure_offset\n                   FROM weather_precision_daily_summary\n                   WHERE date >= ?\n                   ORDER BY date DESC",
                (cutoff_date,),
            )
            if not daily_rows:
                return
            rows_by_date = {}
            for stored_hourly_row in hourly_rows:
                day_str = str(stored_hourly_row[0])
                if day_str not in rows_by_date:
                    rows_by_date[day_str] = []
                rows_by_date[day_str].append(stored_hourly_row)
            all_weather_values = {
                "temperature": [],
                "solar_radiation_wm2": [],
                "clouds": [],
                "humidity": [],
                "wind": [],
                "rain": [],
                "pressure": [],
            }
            clear_weather_values = {
                weather_field: [] for weather_field in all_weather_values.keys()
            }
            cloudy_weather_values = {
                weather_field: [] for weather_field in all_weather_values.keys()
            }
            hourly_solar_values = {}
            hourly_cloud_values = {}
            sample_days = 0
            clear_days = 0
            cloudy_days = 0
            for daily_row in daily_rows:
                day_str = str(daily_row[0])
                hours_tracked = daily_row[1] or 0
                if hours_tracked < MIN_HOURS_FOR_FACTOR:
                    continue
                day_hourly_rows = rows_by_date.get(day_str, [])
                day_cloud_values = []
                for hourly_row in day_hourly_rows:
                    hour = hourly_row[1]
                    forecast_cloud_cover = hourly_row[5]
                    if hour is not None:
                        if 7 <= hour <= 17:
                            if forecast_cloud_cover is not None:
                                day_cloud_values.append(forecast_cloud_cover)
                average_cloud_cover = None
                if day_cloud_values:
                    average_cloud_cover = sum(day_cloud_values) / len(day_cloud_values)
                is_clear_day = (
                    average_cloud_cover is not None
                    and average_cloud_cover <= clear_cloud_threshold
                )
                is_cloudy_day = (
                    average_cloud_cover is not None
                    and average_cloud_cover >= cloudy_cloud_threshold
                )
                temperature_offset = daily_row[2]
                solar_factor = daily_row[3]
                cloud_factor = daily_row[4]
                wind_factor = daily_row[5]
                humidity_factor = daily_row[6]
                rain_difference = daily_row[7]
                pressure_offset = daily_row[8]
                if temperature_offset is not None:
                    all_weather_values["temperature"].append(temperature_offset)
                    if is_clear_day:
                        clear_weather_values["temperature"].append(temperature_offset)
                    elif is_cloudy_day:
                        cloudy_weather_values["temperature"].append(temperature_offset)
                if solar_factor is not None:
                    all_weather_values["solar_radiation_wm2"].append(solar_factor)
                    if is_clear_day:
                        clear_weather_values["solar_radiation_wm2"].append(solar_factor)
                    elif is_cloudy_day:
                        cloudy_weather_values["solar_radiation_wm2"].append(
                            solar_factor
                        )
                if cloud_factor is not None:
                    all_weather_values["clouds"].append(cloud_factor)
                    if is_clear_day:
                        clear_weather_values["clouds"].append(cloud_factor)
                    elif is_cloudy_day:
                        cloudy_weather_values["clouds"].append(cloud_factor)
                if wind_factor is not None:
                    all_weather_values["wind"].append(wind_factor)
                if humidity_factor is not None:
                    all_weather_values["humidity"].append(humidity_factor)
                if rain_difference is not None:
                    all_weather_values["rain"].append(rain_difference)
                if pressure_offset is not None:
                    all_weather_values["pressure"].append(pressure_offset)
                sample_days += 1
                if is_clear_day:
                    clear_days += 1
                elif is_cloudy_day:
                    cloudy_days += 1
                for hourly_row in day_hourly_rows:
                    hour = hourly_row[1]
                    if hour is None or not 7 <= hour <= 17:
                        continue
                    hourly_solar_factor = hourly_row[4]
                    hourly_cloud_factor = hourly_row[7]
                    if hourly_solar_factor is not None:
                        if SOLAR_FACTOR_MIN <= hourly_solar_factor <= SOLAR_FACTOR_MAX:
                            if hour not in hourly_solar_values:
                                hourly_solar_values[hour] = []
                            hourly_solar_values[hour].append(hourly_solar_factor)
                    if hourly_cloud_factor is not None:
                        if 0.1 <= hourly_cloud_factor <= 5.0:
                            if hour not in hourly_cloud_values:
                                hourly_cloud_values[hour] = []
                            hourly_cloud_values[hour].append(hourly_cloud_factor)
                continue
            factors = {}
            confidences = {}
            offset_fields = ["temperature", "pressure"]
            for field, values in all_weather_values.items():
                if values and len(values) >= 3:
                    mean_factor = sum(values) / len(values)
                    factors[field] = round(mean_factor, 3)
                    sample_confidence = min(1.0, len(values) / 7)
                    mean_value = mean_factor
                    variance = sum(
                        ((sample - mean_value) ** 2 for sample in values)
                    ) / len(values)
                    standard_deviation = math_module.sqrt(variance)
                    consistency_confidence = max(0.0, 1.0 - standard_deviation * 2)
                    confidence = (sample_confidence + consistency_confidence) / 2
                    confidences[field] = round(confidence, 2)
                else:
                    if field in offset_fields:
                        factors[field] = 0.0
                    else:
                        factors[field] = 1.0
                    confidences[field] = 0.0
            await self.execute_query(
                "INSERT INTO weather_precision_factors\n                   (id, temperature_factor, solar_factor, cloud_factor,\n                    wind_factor, humidity_factor, rain_factor, pressure_factor,\n                    sample_days, confidence, updated_at)\n                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n                   ON CONFLICT(id) DO UPDATE SET\n                       temperature_factor = excluded.temperature_factor,\n                       solar_factor = excluded.solar_factor,\n                       cloud_factor = excluded.cloud_factor,\n                       wind_factor = excluded.wind_factor,\n                       humidity_factor = excluded.humidity_factor,\n                       rain_factor = excluded.rain_factor,\n                       pressure_factor = excluded.pressure_factor,\n                       sample_days = excluded.sample_days,\n                       confidence = excluded.confidence,\n                       updated_at = CURRENT_TIMESTAMP",
                (
                    factors.get("temperature", 0.0),
                    factors.get("solar_radiation_wm2", 1.0),
                    factors.get("clouds", 1.0),
                    factors.get("wind", 1.0),
                    factors.get("humidity", 1.0),
                    1.0,
                    factors.get("pressure", 0.0),
                    sample_days,
                    confidences.get("solar_radiation_wm2", 0.0),
                ),
            )
            await self._save_hourly_factors(
                hourly_solar_values, hourly_cloud_values, min_hourly_samples
            )
            await self._save_weather_specific_factors(
                clear_weather_values,
                cloudy_weather_values,
                clear_days,
                cloudy_days,
                min_weather_samples,
            )
            _LOGGER.info(
                "Updated rolling factors: %d days (clear=%d, cloudy=%d), solar=%.3f, hourly_hours=%d",
                sample_days,
                clear_days,
                cloudy_days,
                factors.get("solar_radiation_wm2", 1.0),
                len(hourly_solar_values),
            )
        except Exception as error:
            _LOGGER.error("Error updating rolling averages: %s", error, exc_info=True)
        if True:
            return None

    async def get_correction_factors(self) -> dict[str, float]:
        """Get current rolling average correction factors. @zara"""
        try:
            factor_row = await self.fetch_one(
                "SELECT temperature_factor, solar_factor, cloud_factor,\n                          wind_factor, humidity_factor, rain_factor, pressure_factor\n                   FROM weather_precision_factors\n                   WHERE id = 1"
            )
            if factor_row:
                return {
                    "temperature": factor_row[0] or 0.0,
                    "solar_radiation_wm2": factor_row[1] or 1.0,
                    "clouds": factor_row[2] or 1.0,
                    "wind": factor_row[3] or 1.0,
                    "humidity": factor_row[4] or 1.0,
                    "rain": factor_row[5] or 1.0,
                    "pressure": factor_row[6] or 0.0,
                }
            return {
                "temperature": 0.0,
                "solar_radiation_wm2": 1.0,
                "clouds": 1.0,
                "wind": 1.0,
                "humidity": 1.0,
                "rain": 1.0,
                "pressure": 0.0,
            }
        except Exception as error:
            _LOGGER.error("Error getting correction factors: %s", error)
            return {
                "temperature": 0.0,
                "solar_radiation_wm2": 1.0,
                "clouds": 1.0,
                "wind": 1.0,
                "humidity": 1.0,
                "rain": 1.0,
                "pressure": 0.0,
            }

    async def get_confidence_scores(self) -> dict[str, float]:
        """Get current confidence scores. @zara"""
        try:
            confidence_row = await self.fetch_one(
                "SELECT confidence, sample_days FROM weather_precision_factors WHERE id = 1"
            )
            if confidence_row:
                confidence = confidence_row[0] or 0.0
                return {
                    "temperature": confidence,
                    "solar_radiation_wm2": confidence,
                    "clouds": confidence,
                    "wind": confidence,
                    "humidity": confidence,
                    "rain": confidence,
                    "pressure": confidence,
                    "sample_days": confidence_row[1] or 0,
                }
            return {
                "temperature": 0.0,
                "solar_radiation_wm2": 0.0,
                "clouds": 0.0,
                "wind": 0.0,
                "humidity": 0.0,
                "rain": 0.0,
                "pressure": 0.0,
                "sample_days": 0,
            }
        except Exception as error:
            _LOGGER.error("Error getting confidence scores: %s", error)
            return {
                "temperature": 0.0,
                "solar_radiation_wm2": 0.0,
                "clouds": 0.0,
                "wind": 0.0,
                "humidity": 0.0,
                "rain": 0.0,
                "pressure": 0.0,
                "sample_days": 0,
            }

    async def get_precision_summary(self) -> dict[str, Any]:
        """Get summary of precision tracking. @zara"""
        correction_factors = await self.get_correction_factors()
        confidence_scores = await self.get_confidence_scores()
        return {
            "factors": correction_factors,
            "confidence": confidence_scores,
            "sample_days": confidence_scores.get("sample_days", 0),
        }

    async def _save_hourly_factors(
        self,
        hourly_solar: dict[int, list[float]],
        hourly_cloud: dict[int, list[float]],
        min_samples: int,
    ) -> None:
        """Save hourly correction factors to database. @zara"""
        import math as math_module

        try:
            for hour, factor_values in hourly_solar.items():
                if len(factor_values) >= min_samples:
                    mean_factor = sum(factor_values) / len(factor_values)
                    sample_confidence = min(1.0, len(factor_values) / 7)
                    variance = sum(
                        ((sample - mean_factor) ** 2 for sample in factor_values)
                    ) / len(factor_values)
                    standard_deviation = math_module.sqrt(variance)
                    consistency_confidence = max(0.0, 1.0 - standard_deviation * 0.5)
                    confidence = round(
                        (sample_confidence + consistency_confidence) / 2, 2
                    )
                    await self.execute_query(
                        "INSERT INTO weather_precision_hourly_factors\n                           (hour, factor_type, factor_value, sample_count, confidence, std_dev)\n                           VALUES (?, 'solar_radiation_wm2', ?, ?, ?, ?)\n                           ON CONFLICT(hour, factor_type) DO UPDATE SET\n                               factor_value = excluded.factor_value,\n                               sample_count = excluded.sample_count,\n                               confidence = excluded.confidence,\n                               std_dev = excluded.std_dev,\n                               updated_at = CURRENT_TIMESTAMP",
                        (
                            hour,
                            round(mean_factor, 3),
                            len(factor_values),
                            confidence,
                            round(standard_deviation, 3),
                        ),
                    )
            for hour, factor_values in hourly_cloud.items():
                if len(factor_values) >= min_samples:
                    mean_factor = sum(factor_values) / len(factor_values)
                    sample_confidence = min(1.0, len(factor_values) / 7)
                    variance = sum(
                        ((sample - mean_factor) ** 2 for sample in factor_values)
                    ) / len(factor_values)
                    standard_deviation = math_module.sqrt(variance)
                    consistency_confidence = max(0.0, 1.0 - standard_deviation * 0.5)
                    confidence = round(
                        (sample_confidence + consistency_confidence) / 2, 2
                    )
                    await self.execute_query(
                        "INSERT INTO weather_precision_hourly_factors\n                           (hour, factor_type, factor_value, sample_count, confidence, std_dev)\n                           VALUES (?, 'clouds', ?, ?, ?, ?)\n                           ON CONFLICT(hour, factor_type) DO UPDATE SET\n                               factor_value = excluded.factor_value,\n                               sample_count = excluded.sample_count,\n                               confidence = excluded.confidence,\n                               std_dev = excluded.std_dev,\n                               updated_at = CURRENT_TIMESTAMP",
                        (
                            hour,
                            round(mean_factor, 3),
                            len(factor_values),
                            confidence,
                            round(standard_deviation, 3),
                        ),
                    )
        except Exception as error:
            _LOGGER.error("Error saving hourly factors: %s", error)
        return None if True else None

    async def _save_weather_specific_factors(
        self,
        clear_values: dict[str, list[float]],
        cloudy_values: dict[str, list[float]],
        clear_days: int,
        cloudy_days: int,
        min_samples: int,
    ) -> None:
        """Save weather-specific factors to database. @zara"""
        try:
            for field, values in clear_values.items():
                if len(values) >= min_samples:
                    mean_factor = sum(values) / len(values)
                    confidence = min(1.0, len(values) / 7)
                    await self.execute_query(
                        "INSERT INTO weather_precision_weather_specific\n                           (weather_type, factor_type, factor_value, sample_days, confidence)\n                           VALUES ('clear', ?, ?, ?, ?)\n                           ON CONFLICT(weather_type, factor_type) DO UPDATE SET\n                               factor_value = excluded.factor_value,\n                               sample_days = excluded.sample_days,\n                               confidence = excluded.confidence,\n                               updated_at = CURRENT_TIMESTAMP",
                        (
                            field,
                            round(mean_factor, 3),
                            clear_days,
                            round(confidence, 2),
                        ),
                    )
            for field, values in cloudy_values.items():
                if len(values) >= min_samples:
                    mean_factor = sum(values) / len(values)
                    confidence = min(1.0, len(values) / 7)
                    await self.execute_query(
                        "INSERT INTO weather_precision_weather_specific\n                           (weather_type, factor_type, factor_value, sample_days, confidence)\n                           VALUES ('cloudy', ?, ?, ?, ?)\n                           ON CONFLICT(weather_type, factor_type) DO UPDATE SET\n                               factor_value = excluded.factor_value,\n                               sample_days = excluded.sample_days,\n                               confidence = excluded.confidence,\n                               updated_at = CURRENT_TIMESTAMP",
                        (
                            field,
                            round(mean_factor, 3),
                            cloudy_days,
                            round(confidence, 2),
                        ),
                    )
        except Exception as error:
            _LOGGER.error("Error saving weather-specific factors: %s", error)
        return None if True else None

    async def get_hourly_correction_factors(self) -> dict[str, dict[str, Any]]:
        """Get hourly correction factors for solar_radiation_wm2 and clouds. @zara

        V16.1: Returns hourly factors that override daily factors for specific hours.

        Returns:
            Dict with structure:
            {
                "solar_radiation_wm2": {
                    "9": {"factor": 1.282, "samples": 10, "confidence": 0.65},
                    ...
                },
                "clouds": {...},
                "min_samples": 3
            }
        """
        try:
            rows = await self.fetch_all(
                "SELECT hour, factor_type, factor_value, sample_count, confidence, std_dev\n                   FROM weather_precision_hourly_factors\n                   WHERE factor_type IN ('solar_radiation_wm2', 'clouds')"
            )
            result = {"solar_radiation_wm2": {}, "clouds": {}, "min_samples": 3}
            for row in rows:
                (
                    hour,
                    factor_type,
                    factor_value,
                    sample_count,
                    confidence,
                    standard_deviation,
                ) = row
                if factor_type in result:
                    result[factor_type][str(hour)] = {
                        "factor": factor_value,
                        "samples": sample_count,
                        "confidence": confidence or 0.0,
                        "std_dev": standard_deviation,
                    }
            return result
        except Exception as error:
            _LOGGER.error("Error getting hourly correction factors: %s", error)
            return {"solar_radiation_wm2": {}, "clouds": {}, "min_samples": 3}

    def get_hourly_factor(
        self,
        hourly_factors: dict[str, dict[str, Any]],
        field: str,
        hour: int,
        daily_fallback: float,
    ) -> float:
        """Get correction factor for a specific field and hour. @zara

        V16.1: Helper method to get the best available factor:
        1. If hourly factor exists for this hour with sufficient samples -> use it
        2. Otherwise -> use daily fallback

        Args:
            hourly_factors: Result from get_hourly_correction_factors()
            field: Field name (e.g., "solar_radiation_wm2")
            hour: Hour (0-23)
            daily_fallback: Daily average factor to use if no hourly factor

        Returns:
            Correction factor (float)
        """
        factors_by_hour = hourly_factors.get(field, {})
        hour_factor = factors_by_hour.get(str(hour))
        if hour_factor and "factor" in hour_factor:
            return hour_factor["factor"]
        return daily_fallback

    async def get_weather_specific_factors(
        self, weather_type: str = "mixed"
    ) -> dict[str, float]:
        """Get correction factors for specific weather type. @zara

        V16.1: Returns factors learned specifically for clear or cloudy days.

        Args:
            weather_type: "clear", "cloudy", or "mixed" (falls back to daily average)

        Returns:
            Dict with factor values for each field
        """
        try:
            if weather_type not in ("clear", "cloudy"):
                return await self.get_correction_factors()
            rows = await self.fetch_all(
                "SELECT factor_type, factor_value, confidence\n                   FROM weather_precision_weather_specific\n                   WHERE weather_type = ?",
                (weather_type,),
            )
            if not rows:
                return await self.get_correction_factors()
            result = {
                "temperature": 0.0,
                "solar_radiation_wm2": 1.0,
                "clouds": 1.0,
                "wind": 1.0,
                "humidity": 1.0,
                "rain": 1.0,
                "pressure": 0.0,
            }
            for row in rows:
                factor_type, factor_value, confidence = row
                if factor_type in result and confidence and (confidence >= 0.3):
                    result[factor_type] = factor_value
            return result
        except Exception as error:
            _LOGGER.error("Error getting weather-specific factors: %s", error)
            return await self.get_correction_factors()
