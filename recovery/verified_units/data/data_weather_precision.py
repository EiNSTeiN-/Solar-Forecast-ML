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
import math
from datetime import timedelta
from typing import Any, Optional
from homeassistant.core import HomeAssistant
from .db_manager import (
    DatabaseManager,
    WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION,
    WEATHER_FORECAST_USAGE_CONTRACT_VERSION,
)
from .data_io import DataManagerIO
from .data_weather_radiation_ensemble import (
    resolve_preserved_solar_precision_state,
    solar_precision_window_is_apply_ready,
)
from ..core.core_helpers import SafeDateTimeUtil

_LOGGER = logging.getLogger(__name__)
ROLLING_WINDOW_DAYS = 7
MIN_HOURS_FOR_FACTOR = 4
SOLAR_FACTOR_MIN = 0.25
SOLAR_FACTOR_MAX = 2.0
SOLAR_MIN_FORECAST_WM2 = 20.0
MIN_SOLAR_HOURS_FOR_FACTOR = 3


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
                CONF_TEMP_SENSOR as CONF_TEMP_SENSOR,
                CONF_HUMIDITY_SENSOR as CONF_HUMIDITY_SENSOR,
                CONF_WIND_SENSOR as CONF_WIND_SENSOR,
                CONF_RAIN_SENSOR as CONF_RAIN_SENSOR,
                CONF_PRESSURE_SENSOR as CONF_PRESSURE_SENSOR,
                CONF_SOLAR_RADIATION_SENSOR as CONF_SOLAR_RADIATION_SENSOR,
            )

            self.sensors = {
                "temp": config_entry.data.get(CONF_TEMP_SENSOR),
                "solar_radiation_wm2": config_entry.data.get(
                    CONF_SOLAR_RADIATION_SENSOR
                ),
                "humidity": config_entry.data.get(CONF_HUMIDITY_SENSOR),
                "wind": config_entry.data.get(CONF_WIND_SENSOR),
                "rain": config_entry.data.get(CONF_RAIN_SENSOR),
                "pressure": config_entry.data.get(CONF_PRESSURE_SENSOR),
            }
        configured_sensors = [
            sensor_name
            for sensor_name, sensor_entity in self.sensors.items()
            if sensor_entity
        ]
        _LOGGER.debug(
            "WeatherPrecisionTracker initialized with sensors: %s", configured_sensors
        )

    async def async_init(self) -> bool:
        """Initialize the weather precision tracker. @zara"""
        await self.ensure_initialized()
        return True

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
                date_str = SafeDateTimeUtil.now().strftime("%Y-%m-%d")
            _LOGGER.info("Calculating correction factors for %s", date_str)
            actual_weather_by_hour = (
                await weather_actual_tracker.get_daily_actual_weather(date_str)
            )
            if not actual_weather_by_hour:
                _LOGGER.info("No actual weather data for %s", date_str)
                return False
            _LOGGER.info(
                "Loaded actual weather for %d hours", len(actual_weather_by_hour)
            )
            raw_forecasts_by_hour = await self._load_raw_forecasts(date_str)
            if not raw_forecasts_by_hour:
                _LOGGER.debug("No raw forecast data for %s", date_str)
                return False
            _LOGGER.info(
                "Loaded raw forecasts for %d hours", len(raw_forecasts_by_hour)
            )
            hourly_comparisons = []
            for hour, actual_weather in actual_weather_by_hour.items():
                if hour not in raw_forecasts_by_hour:
                    continue
                forecast_weather = raw_forecasts_by_hour[hour]
                hourly_comparison = self._compare_weather_fields(
                    forecast_weather, actual_weather, hour
                )
                if hourly_comparison:
                    hourly_comparisons.append(hourly_comparison)
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
        """Load frozen morning weather provenance, never the mutable cache."""
        try:
            forecast_rows = await self.fetch_all(
                "SELECT u.target_hour,\n                          c.temperature, c.humidity, c.clouds, c.rain, c.wind,\n                          c.pressure, COALESCE(c.ensemble_ghi, c.raw_blend_ghi),\n                          c.final_used_ghi, u.usage_snapshot_id,\n                          c.contract_version, c.observation_mode,\n                          c.local_observation_available,\n                          c.cloud_cover_low, c.cloud_cover_mid,\n                          c.cloud_cover_high, c.visibility_m, c.sun_elevation\n                   FROM weather_forecast_usage_snapshots u\n                   JOIN hourly_predictions hp\n                     ON hp.prediction_id = u.prediction_id\n                    AND hp.morning_batch_id = u.morning_batch_id\n                   JOIN weather_forecast_correction_snapshots c\n                     ON c.correction_snapshot_id = u.correction_snapshot_id\n                   WHERE u.target_date = ?\n                     AND u.contract_version = ?\n                     AND c.contract_version = ?\n                   ORDER BY u.target_hour",
                (
                    date_str,
                    WEATHER_FORECAST_USAGE_CONTRACT_VERSION,
                    WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION,
                ),
            )
            forecasts_by_hour = {}
            for forecast_row in forecast_rows:
                forecasts_by_hour[forecast_row[0]] = {
                    "temperature": forecast_row[1],
                    "humidity": forecast_row[2],
                    "cloud_cover": forecast_row[3],
                    "precipitation": forecast_row[4],
                    "wind_speed": forecast_row[5],
                    "pressure": forecast_row[6],
                    "ghi": forecast_row[7],
                    "final_used_ghi": forecast_row[8],
                    "usage_snapshot_id": forecast_row[9],
                    "forecast_contract_version": forecast_row[10],
                    "observation_mode": forecast_row[11],
                    "local_observation_available": bool(forecast_row[12]),
                    "cloud_cover_low": forecast_row[13],
                    "cloud_cover_mid": forecast_row[14],
                    "cloud_cover_high": forecast_row[15],
                    "visibility_m": forecast_row[16],
                    "sun_elevation": forecast_row[17],
                }
            return forecasts_by_hour
        except Exception as error:
            _LOGGER.error("Error loading raw forecasts: %s", error)
            return {}

    def _compare_weather_fields(
        self, forecast: dict[str, Any], actual: dict[str, Any], hour: int
    ) -> Optional[dict[str, Any]]:
        """Compare forecast vs actual for a single hour. @zara"""
        hourly_comparison = {"hour": hour, "fields": {}}
        compared_field_count = 0
        forecast_temperature_c = forecast.get("temperature")
        actual_temperature_c = actual.get("temperature_c")
        if forecast_temperature_c is not None and actual_temperature_c is not None:
            temperature_offset_c = actual_temperature_c - forecast_temperature_c
            hourly_comparison["fields"]["temperature"] = {
                "forecast": forecast_temperature_c,
                "actual": actual_temperature_c,
                "offset": round(temperature_offset_c, 2),
            }
            compared_field_count += 1
        forecast_ghi_wm2 = forecast.get("ghi")
        actual_ghi_wm2 = actual.get("solar_radiation_wm2")
        has_measured_solar_comparison = (
            forecast.get("forecast_contract_version")
            == WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION
            and forecast.get("observation_mode") == "measured_local"
            and bool(forecast.get("local_observation_available"))
            and (forecast_ghi_wm2 is not None)
            and (actual_ghi_wm2 is not None)
            and (forecast_ghi_wm2 >= SOLAR_MIN_FORECAST_WM2)
        )
        if has_measured_solar_comparison:
            solar_factor = actual_ghi_wm2 / forecast_ghi_wm2
            hourly_comparison["fields"]["solar_radiation_wm2"] = {
                "forecast": forecast_ghi_wm2,
                "actual": actual_ghi_wm2,
                "factor": round(solar_factor, 3),
                "final_used_forecast": forecast.get("final_used_ghi"),
            }
            compared_field_count += 1
        forecast_cloud_cover_percent = forecast.get("cloud_cover")
        actual_cloud_cover_percent = actual.get("cloud_cover_percent")
        if (
            forecast_cloud_cover_percent is not None
            and actual_cloud_cover_percent is not None
            and (forecast_cloud_cover_percent > 0)
            and (actual.get("cloud_cover_source") == "sensor_calculated")
        ):
            solar_factor = actual_cloud_cover_percent / forecast_cloud_cover_percent
            hourly_comparison["fields"]["clouds"] = {
                "forecast": forecast_cloud_cover_percent,
                "actual": actual_cloud_cover_percent,
                "factor": round(solar_factor, 3),
            }
            compared_field_count += 1
        forecast_humidity_percent = forecast.get("humidity")
        actual_humidity_percent = actual.get("humidity_percent")
        if (
            forecast_humidity_percent is not None
            and actual_humidity_percent is not None
            and (forecast_humidity_percent > 0)
        ):
            solar_factor = actual_humidity_percent / forecast_humidity_percent
            hourly_comparison["fields"]["humidity"] = {
                "forecast": forecast_humidity_percent,
                "actual": actual_humidity_percent,
                "factor": round(solar_factor, 3),
            }
            compared_field_count += 1
        forecast_wind_speed_ms = forecast.get("wind_speed")
        actual_wind_speed_ms = actual.get("wind_speed_ms")
        if (
            forecast_wind_speed_ms is not None
            and actual_wind_speed_ms is not None
            and (forecast_wind_speed_ms > 0)
        ):
            solar_factor = actual_wind_speed_ms / forecast_wind_speed_ms
            hourly_comparison["fields"]["wind"] = {
                "forecast": forecast_wind_speed_ms,
                "actual": actual_wind_speed_ms,
                "factor": round(solar_factor, 3),
            }
            compared_field_count += 1
        forecast_precipitation_mm = forecast.get("precipitation") or 0
        actual_precipitation_mm = actual.get("precipitation_mm") or 0
        if (
            forecast_precipitation_mm is not None
            and actual_precipitation_mm is not None
        ):
            rain_difference_mm = actual_precipitation_mm - forecast_precipitation_mm
            hourly_comparison["fields"]["rain"] = {
                "forecast": forecast_precipitation_mm,
                "actual": actual_precipitation_mm,
                "difference": round(rain_difference_mm, 2),
            }
            compared_field_count += 1
        forecast_pressure_hpa = forecast.get("pressure")
        actual_pressure_hpa = actual.get("pressure_hpa")
        if forecast_pressure_hpa is not None and actual_pressure_hpa is not None:
            temperature_offset_c = actual_pressure_hpa - forecast_pressure_hpa
            hourly_comparison["fields"]["pressure"] = {
                "forecast": forecast_pressure_hpa,
                "actual": actual_pressure_hpa,
                "offset": round(temperature_offset_c, 2),
            }
            compared_field_count += 1
        return hourly_comparison if compared_field_count > 0 else None

    def _calculate_daily_factors(
        self, hourly_comparisons: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Calculate robust daily factors and explicit GHI error metrics."""
        offset_samples_by_field = {"temperature": [], "pressure": []}
        factor_samples_by_field = {"clouds": [], "humidity": [], "wind": []}
        ghi_samples = []
        difference_samples_by_field = {"rain": []}
        for hourly_comparison in hourly_comparisons:
            comparison_fields = hourly_comparison.get("fields", {})
            for comparison_field_name, field_comparison in comparison_fields.items():
                if (
                    comparison_field_name in offset_samples_by_field
                    and "offset" in field_comparison
                ):
                    offset = field_comparison["offset"]
                    if -20.0 <= offset <= 20.0:
                        offset_samples_by_field[comparison_field_name].append(offset)
                elif comparison_field_name == "solar_radiation_wm2":
                    ghi_samples.append(
                        (
                            float(field_comparison["forecast"]),
                            float(field_comparison["actual"]),
                            field_comparison.get("final_used_forecast"),
                        )
                    )
                elif (
                    comparison_field_name in factor_samples_by_field
                    and "factor" in field_comparison
                ):
                    factor = field_comparison["factor"]
                    factor_bounds = {
                        "clouds": (0.1, 5.0),
                        "humidity": (0.5, 1.5),
                        "wind": (0.1, 5.0),
                    }[comparison_field_name]
                    if factor_bounds[0] <= factor <= factor_bounds[1]:
                        factor_samples_by_field[comparison_field_name].append(factor)
                elif (
                    comparison_field_name in difference_samples_by_field
                    and "difference" in field_comparison
                ):
                    rain_difference_mm = field_comparison["difference"]
                    if -50.0 <= rain_difference_mm <= 50.0:
                        difference_samples_by_field[comparison_field_name].append(
                            rain_difference_mm
                        )
        daily_factors = {}
        if len(ghi_samples) >= MIN_SOLAR_HOURS_FOR_FACTOR:
            total_forecast_ghi_wm2 = sum((ghi_sample[0] for ghi_sample in ghi_samples))
            total_actual_ghi_wm2 = sum(
                (max(0.0, ghi_sample[1]) for ghi_sample in ghi_samples)
            )
            solar_factor = (
                total_actual_ghi_wm2 / total_forecast_ghi_wm2
                if total_forecast_ghi_wm2 > 0
                else 1.0
            )
            daily_factors["solar_radiation_wm2_factor"] = round(
                max(SOLAR_FACTOR_MIN, min(SOLAR_FACTOR_MAX, solar_factor)), 3
            )
        else:
            daily_factors["solar_radiation_wm2_factor"] = 1.0
        daily_factors["solar_sample_hours"] = len(ghi_samples)
        final_forecast_actual_pairs = [
            (float(final_used_forecast_ghi_wm2), actual_ghi_wm2)
            for raw_forecast_ghi_wm2, actual_ghi_wm2, final_used_forecast_ghi_wm2 in ghi_samples
            if isinstance(final_used_forecast_ghi_wm2, (int, float))
            and math.isfinite(float(final_used_forecast_ghi_wm2))
        ]
        if final_forecast_actual_pairs:
            ghi_errors_wm2 = [
                final_forecast_ghi_wm2 - actual_ghi_wm2
                for final_forecast_ghi_wm2, actual_ghi_wm2 in final_forecast_actual_pairs
            ]
            absolute_ghi_errors_wm2 = [
                abs(ghi_error_wm2) for ghi_error_wm2 in ghi_errors_wm2
            ]
            ghi_normalization_total = sum(
                (
                    max(abs(final_forecast_ghi_wm2), abs(actual_ghi_wm2))
                    for final_forecast_ghi_wm2, actual_ghi_wm2 in final_forecast_actual_pairs
                )
            )
            daily_factors["ghi_mae_wm2"] = round(
                sum(absolute_ghi_errors_wm2) / len(ghi_errors_wm2), 2
            )
            daily_factors["ghi_rmse_wm2"] = round(
                math.sqrt(
                    sum(
                        (
                            ghi_error_wm2 * ghi_error_wm2
                            for ghi_error_wm2 in ghi_errors_wm2
                        )
                    )
                    / len(ghi_errors_wm2)
                ),
                2,
            )
            daily_factors["ghi_bias_wm2"] = round(
                sum(ghi_errors_wm2) / len(ghi_errors_wm2), 2
            )
            daily_factors["ghi_nmae_percent"] = round(
                (
                    100.0 * sum(absolute_ghi_errors_wm2) / ghi_normalization_total
                    if ghi_normalization_total > 0
                    else 0.0
                ),
                2,
            )
            daily_factors["weather_metric_basis"] = "measured_ghi_frozen_morning"
        else:
            daily_factors["ghi_mae_wm2"] = None
            daily_factors["ghi_rmse_wm2"] = None
            daily_factors["ghi_bias_wm2"] = None
            daily_factors["ghi_nmae_percent"] = None
            daily_factors["weather_metric_basis"] = "provider_only_no_local_ghi"
        for factor_field_name, field_samples in offset_samples_by_field.items():
            if field_samples:
                daily_factors[f"{factor_field_name}_offset"] = round(
                    sum(field_samples) / len(field_samples), 2
                )
            else:
                daily_factors[f"{factor_field_name}_offset"] = 0.0
        for factor_field_name, field_samples in factor_samples_by_field.items():
            if field_samples:
                daily_factors[f"{factor_field_name}_factor"] = round(
                    sum(field_samples) / len(field_samples), 3
                )
            else:
                daily_factors[f"{factor_field_name}_factor"] = 1.0
        for factor_field_name, field_samples in difference_samples_by_field.items():
            if field_samples:
                daily_factors[f"{factor_field_name}_diff"] = round(
                    sum(field_samples) / len(field_samples), 2
                )
            else:
                daily_factors[f"{factor_field_name}_diff"] = 0.0
        return daily_factors

    async def _save_hourly_comparison(
        self, date_str: str, hour: int, forecast: dict[str, Any], actual: dict[str, Any]
    ) -> None:
        try:
            forecast_temperature_c = forecast.get("temperature")
            actual_temperature_c = actual.get("temperature_c")
            temperature_offset_c = (
                actual_temperature_c - forecast_temperature_c
                if forecast_temperature_c is not None
                and actual_temperature_c is not None
                else None
            )
            forecast_humidity_percent = forecast.get("humidity")
            actual_humidity_percent = actual.get("humidity_percent")
            humidity_factor = (
                actual_humidity_percent / forecast_humidity_percent
                if forecast_humidity_percent and actual_humidity_percent
                else None
            )
            forecast_wind_speed_ms = forecast.get("wind_speed")
            actual_wind_speed_ms = actual.get("wind_speed_ms")
            wind_factor = (
                actual_wind_speed_ms / forecast_wind_speed_ms
                if forecast_wind_speed_ms and actual_wind_speed_ms
                else None
            )
            forecast_precipitation_mm = forecast.get("precipitation")
            actual_precipitation_mm = actual.get("precipitation_mm")
            rain_difference_mm = (
                actual_precipitation_mm - forecast_precipitation_mm
                if forecast_precipitation_mm is not None
                and actual_precipitation_mm is not None
                else None
            )
            forecast_pressure_hpa = forecast.get("pressure")
            actual_pressure_hpa = actual.get("pressure_hpa")
            pressure_offset_hpa = (
                actual_pressure_hpa - forecast_pressure_hpa
                if forecast_pressure_hpa is not None and actual_pressure_hpa is not None
                else None
            )
            forecast_ghi_wm2 = forecast.get("ghi")
            actual_ghi_wm2 = actual.get("solar_radiation_wm2")
            has_measured_solar_sample = (
                forecast.get("forecast_contract_version")
                == WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION
                and forecast.get("observation_mode") == "measured_local"
                and bool(forecast.get("local_observation_available"))
                and isinstance(forecast_ghi_wm2, (int, float))
                and (forecast_ghi_wm2 >= SOLAR_MIN_FORECAST_WM2)
                and isinstance(actual_ghi_wm2, (int, float))
            )
            solar_factor = (
                max(
                    SOLAR_FACTOR_MIN,
                    min(SOLAR_FACTOR_MAX, actual_ghi_wm2 / forecast_ghi_wm2),
                )
                if has_measured_solar_sample
                else None
            )
            if not forecast.get("usage_snapshot_id"):
                solar_factor_skip_reason = "missing_frozen_usage_snapshot"
            elif forecast.get("observation_mode") != "measured_local":
                solar_factor_skip_reason = "provider_only_no_local_ghi"
            elif actual_ghi_wm2 is None:
                solar_factor_skip_reason = "missing_measured_ghi"
            elif (
                not isinstance(forecast_ghi_wm2, (int, float))
                or forecast_ghi_wm2 < SOLAR_MIN_FORECAST_WM2
            ):
                solar_factor_skip_reason = "insufficient_daylight_ghi"
            else:
                solar_factor_skip_reason = None
            forecast_cloud_cover_percent = forecast.get("cloud_cover")
            actual_cloud_cover_percent = actual.get("cloud_cover_percent")
            cloud_factor = (
                actual_cloud_cover_percent / forecast_cloud_cover_percent
                if forecast_cloud_cover_percent
                and actual_cloud_cover_percent is not None
                and (forecast_cloud_cover_percent > 0)
                and (actual.get("cloud_cover_source") == "sensor_calculated")
                else None
            )
            await self.execute_query(
                "INSERT INTO weather_precision_daily\n                   (date, hour, temp_forecast, temp_actual, temp_offset,\n                    humidity_forecast, humidity_actual, humidity_factor,\n                    wind_forecast, wind_actual, wind_factor,\n                    rain_forecast, rain_actual, rain_difference,\n                    pressure_forecast, pressure_actual, pressure_offset,\n                    solar_forecast, solar_actual, solar_factor,\n                    clouds_forecast, clouds_actual, clouds_factor,\n                    usage_snapshot_id, forecast_contract_version,\n                    solar_sample_eligible, solar_exclusion_reason)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(date, hour) DO UPDATE SET\n                       temp_forecast = excluded.temp_forecast,\n                       temp_actual = excluded.temp_actual,\n                       temp_offset = excluded.temp_offset,\n                       humidity_forecast = excluded.humidity_forecast,\n                       humidity_actual = excluded.humidity_actual,\n                       humidity_factor = excluded.humidity_factor,\n                       wind_forecast = excluded.wind_forecast,\n                       wind_actual = excluded.wind_actual,\n                       wind_factor = excluded.wind_factor,\n                       rain_forecast = excluded.rain_forecast,\n                       rain_actual = excluded.rain_actual,\n                       rain_difference = excluded.rain_difference,\n                       pressure_forecast = excluded.pressure_forecast,\n                       pressure_actual = excluded.pressure_actual,\n                       pressure_offset = excluded.pressure_offset,\n                       solar_forecast = excluded.solar_forecast,\n                       solar_actual = excluded.solar_actual,\n                       solar_factor = excluded.solar_factor,\n                       clouds_forecast = excluded.clouds_forecast,\n                       clouds_actual = excluded.clouds_actual,\n                       clouds_factor = excluded.clouds_factor,\n                       usage_snapshot_id = excluded.usage_snapshot_id,\n                       forecast_contract_version = excluded.forecast_contract_version,\n                       solar_sample_eligible = excluded.solar_sample_eligible,\n                       solar_exclusion_reason = excluded.solar_exclusion_reason",
                (
                    date_str,
                    hour,
                    forecast_temperature_c,
                    actual_temperature_c,
                    temperature_offset_c,
                    forecast_humidity_percent,
                    actual_humidity_percent,
                    humidity_factor,
                    forecast_wind_speed_ms,
                    actual_wind_speed_ms,
                    wind_factor,
                    forecast_precipitation_mm,
                    actual_precipitation_mm,
                    rain_difference_mm,
                    forecast_pressure_hpa,
                    actual_pressure_hpa,
                    pressure_offset_hpa,
                    forecast_ghi_wm2,
                    actual_ghi_wm2,
                    solar_factor,
                    forecast_cloud_cover_percent,
                    actual_cloud_cover_percent,
                    cloud_factor,
                    forecast.get("usage_snapshot_id"),
                    forecast.get("forecast_contract_version"),
                    int(has_measured_solar_sample),
                    solar_factor_skip_reason,
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
                "INSERT INTO weather_precision_daily_summary\n                   (date, hours_tracked, avg_temp_offset, avg_solar_factor,\n                    avg_clouds_factor, avg_wind_factor, avg_humidity_factor,\n                    avg_rain_diff, avg_pressure_offset, solar_sample_hours,\n                    ghi_mae_wm2, ghi_rmse_wm2, ghi_bias_wm2,\n                    ghi_nmae_percent, weather_metric_basis,\n                    forecast_contract_version)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(date) DO UPDATE SET\n                       hours_tracked = excluded.hours_tracked,\n                       avg_temp_offset = excluded.avg_temp_offset,\n                       avg_solar_factor = excluded.avg_solar_factor,\n                       avg_clouds_factor = excluded.avg_clouds_factor,\n                       avg_wind_factor = excluded.avg_wind_factor,\n                       avg_humidity_factor = excluded.avg_humidity_factor,\n                       avg_rain_diff = excluded.avg_rain_diff,\n                       avg_pressure_offset = excluded.avg_pressure_offset,\n                       solar_sample_hours = excluded.solar_sample_hours,\n                       ghi_mae_wm2 = excluded.ghi_mae_wm2,\n                       ghi_rmse_wm2 = excluded.ghi_rmse_wm2,\n                       ghi_bias_wm2 = excluded.ghi_bias_wm2,\n                       ghi_nmae_percent = excluded.ghi_nmae_percent,\n                       weather_metric_basis = excluded.weather_metric_basis,\n                       forecast_contract_version = excluded.forecast_contract_version",
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
                    factors.get("solar_sample_hours", 0),
                    factors.get("ghi_mae_wm2"),
                    factors.get("ghi_rmse_wm2"),
                    factors.get("ghi_bias_wm2"),
                    factors.get("ghi_nmae_percent"),
                    factors.get("weather_metric_basis"),
                    WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION,
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
        try:
            clear_day_cloud_threshold = 30
            cloudy_day_cloud_threshold = 50
            hourly_min_samples = 3
            weather_min_samples = 2
            window_start_date = (
                (SafeDateTimeUtil.now if True else SafeDateTimeUtil.now)()
                - timedelta(days=ROLLING_WINDOW_DAYS - 1)
            ).strftime("%Y-%m-%d")
            legacy_summary_rows = await self.fetch_all(
                "SELECT ds.date\n                   FROM weather_precision_daily_summary ds\n                   WHERE ds.date >= ?\n                     AND ds.forecast_contract_version IS NULL\n                     AND ds.avg_solar_factor = 1.0\n                     AND ds.avg_temp_offset = 0.0",
                (window_start_date,),
            )
            if legacy_summary_rows:
                for legacy_summary_row in legacy_summary_rows:
                    legacy_summary_date = str(legacy_summary_row[0])
                    legacy_hourly_rows = await self.fetch_all(
                        "SELECT solar_factor, temp_offset, clouds_factor,\n                                  wind_factor, humidity_factor, rain_difference,\n                                  pressure_offset\n                           FROM weather_precision_daily\n                           WHERE date = ? AND solar_factor IS NOT NULL",
                        (legacy_summary_date,),
                    )
                    if legacy_hourly_rows:
                        legacy_solar_factors = [
                            legacy_hourly_row[0]
                            for legacy_hourly_row in legacy_hourly_rows
                            if legacy_hourly_row[0] is not None
                            if SOLAR_FACTOR_MIN
                            <= legacy_hourly_row[0]
                            <= SOLAR_FACTOR_MAX
                        ]
                        legacy_temperature_offsets = [
                            legacy_hourly_row[1]
                            for legacy_hourly_row in legacy_hourly_rows
                            if legacy_hourly_row[1] is not None
                            if -20 <= legacy_hourly_row[1] <= 20
                        ]
                        legacy_cloud_factors = [
                            legacy_hourly_row[2]
                            for legacy_hourly_row in legacy_hourly_rows
                            if legacy_hourly_row[2] is not None
                            if 0.1 <= legacy_hourly_row[2] <= 10.0
                        ]
                        legacy_wind_factors = [
                            legacy_hourly_row[3]
                            for legacy_hourly_row in legacy_hourly_rows
                            if legacy_hourly_row[3] is not None
                            if 0.1 <= legacy_hourly_row[3] <= 10.0
                        ]
                        legacy_humidity_factors = [
                            legacy_hourly_row[4]
                            for legacy_hourly_row in legacy_hourly_rows
                            if legacy_hourly_row[4] is not None
                            if 0.1 <= legacy_hourly_row[4] <= 10.0
                        ]
                        legacy_rain_differences = [
                            legacy_hourly_row[5]
                            for legacy_hourly_row in legacy_hourly_rows
                            if legacy_hourly_row[5] is not None
                        ]
                        legacy_pressure_offsets = [
                            legacy_hourly_row[6]
                            for legacy_hourly_row in legacy_hourly_rows
                            if legacy_hourly_row[6] is not None
                        ]
                        await self.execute_query(
                            "UPDATE weather_precision_daily_summary\n                               SET avg_solar_factor = ?,\n                                   avg_temp_offset = ?,\n                                   avg_clouds_factor = ?,\n                                   avg_wind_factor = ?,\n                                   avg_humidity_factor = ?,\n                                   avg_rain_diff = ?,\n                                   avg_pressure_offset = ?\n                               WHERE date = ?",
                            (
                                (
                                    round(
                                        sum(legacy_solar_factors)
                                        / len(legacy_solar_factors),
                                        3,
                                    )
                                    if legacy_solar_factors
                                    else 1.0
                                ),
                                (
                                    round(
                                        sum(legacy_temperature_offsets)
                                        / len(legacy_temperature_offsets),
                                        2,
                                    )
                                    if legacy_temperature_offsets
                                    else 0.0
                                ),
                                (
                                    round(
                                        sum(legacy_cloud_factors)
                                        / len(legacy_cloud_factors),
                                        3,
                                    )
                                    if legacy_cloud_factors
                                    else 1.0
                                ),
                                (
                                    round(
                                        sum(legacy_wind_factors)
                                        / len(legacy_wind_factors),
                                        3,
                                    )
                                    if legacy_wind_factors
                                    else 1.0
                                ),
                                (
                                    round(
                                        sum(legacy_humidity_factors)
                                        / len(legacy_humidity_factors),
                                        3,
                                    )
                                    if legacy_humidity_factors
                                    else 1.0
                                ),
                                (
                                    round(
                                        sum(legacy_rain_differences)
                                        / len(legacy_rain_differences),
                                        2,
                                    )
                                    if legacy_rain_differences
                                    else 0.0
                                ),
                                (
                                    round(
                                        sum(legacy_pressure_offsets)
                                        / len(legacy_pressure_offsets),
                                        2,
                                    )
                                    if legacy_pressure_offsets
                                    else 0.0
                                ),
                                legacy_summary_date,
                            ),
                        )
                        continue
                _LOGGER.info(
                    "Repaired %d daily_summary rows from hourly data",
                    len(legacy_summary_rows),
                )
            hourly_precision_rows = await self.fetch_all(
                "SELECT date, hour, solar_forecast, solar_actual, solar_factor,\n                          clouds_forecast, clouds_actual, clouds_factor,\n                          temp_forecast, temp_actual, temp_offset,\n                          humidity_forecast, humidity_actual, humidity_factor,\n                          wind_forecast, wind_actual, wind_factor,\n                          rain_forecast, rain_actual, rain_difference,\n                          pressure_forecast, pressure_actual, pressure_offset,\n                          solar_sample_eligible\n                   FROM weather_precision_daily\n                   WHERE date >= ?\n                     AND forecast_contract_version = ?\n                   ORDER BY date, hour",
                (window_start_date, WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION),
            )
            daily_summary_rows = await self.fetch_all(
                "SELECT date, hours_tracked, avg_temp_offset, avg_solar_factor,\n                          avg_clouds_factor, avg_wind_factor, avg_humidity_factor,\n                          avg_rain_diff, avg_pressure_offset, solar_sample_hours\n                   FROM weather_precision_daily_summary\n                   WHERE date >= ?\n                     AND forecast_contract_version = ?\n                   ORDER BY date DESC",
                (window_start_date, WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION),
            )
            if not daily_summary_rows:
                return
            hourly_precision_by_date = {}
            for hourly_row in hourly_precision_rows:
                summary_date = str(hourly_row[0])
                if summary_date not in hourly_precision_by_date:
                    hourly_precision_by_date[summary_date] = []
                hourly_precision_by_date[summary_date].append(hourly_row)
            daily_values_by_field = {
                "temperature": [],
                "solar_radiation_wm2": [],
                "clouds": [],
                "humidity": [],
                "wind": [],
                "rain": [],
                "pressure": [],
            }
            clear_values_by_field = {
                field_name: [] for field_name in daily_values_by_field.keys()
            }
            cloudy_values_by_field = {
                field_name: [] for field_name in daily_values_by_field.keys()
            }
            hourly_solar_factors = {}
            hourly_cloud_factors = {}
            sample_days = 0
            solar_sample_days = 0
            clear_sample_days = 0
            cloudy_sample_days = 0
            for daily_summary_row in daily_summary_rows:
                summary_date = str(daily_summary_row[0])
                hours_tracked = daily_summary_row[1] or 0
                if hours_tracked < MIN_HOURS_FOR_FACTOR:
                    continue
                day_hourly_rows = hourly_precision_by_date.get(summary_date, [])
                cloud_forecasts = []
                for hourly_precision_row in day_hourly_rows:
                    hour = hourly_precision_row[1]
                    hourly_cloud_forecast = hourly_precision_row[5]
                    if hour is not None:
                        if 7 <= hour <= 17:
                            if hourly_cloud_forecast is not None:
                                cloud_forecasts.append(hourly_cloud_forecast)
                average_cloud_forecast = None
                if cloud_forecasts:
                    average_cloud_forecast = sum(cloud_forecasts) / len(cloud_forecasts)
                is_clear_day = (
                    average_cloud_forecast is not None
                    and average_cloud_forecast <= clear_day_cloud_threshold
                )
                is_cloudy_day = (
                    average_cloud_forecast is not None
                    and average_cloud_forecast >= cloudy_day_cloud_threshold
                )
                daily_temperature_offset_c = daily_summary_row[2]
                solar_factor = daily_summary_row[3]
                cloud_factor = daily_summary_row[4]
                daily_wind_factor = daily_summary_row[5]
                daily_humidity_factor = daily_summary_row[6]
                rain_difference_mm = daily_summary_row[7]
                daily_pressure_offset_hpa = daily_summary_row[8]
                solar_hours_tracked = daily_summary_row[9] or 0
                if daily_temperature_offset_c is not None:
                    daily_values_by_field["temperature"].append(
                        daily_temperature_offset_c
                    )
                    if is_clear_day:
                        clear_values_by_field["temperature"].append(
                            daily_temperature_offset_c
                        )
                    elif is_cloudy_day:
                        cloudy_values_by_field["temperature"].append(
                            daily_temperature_offset_c
                        )
                if (
                    solar_factor is not None
                    and solar_hours_tracked >= MIN_SOLAR_HOURS_FOR_FACTOR
                ):
                    daily_values_by_field["solar_radiation_wm2"].append(solar_factor)
                    solar_sample_days += 1
                    if is_clear_day:
                        clear_values_by_field["solar_radiation_wm2"].append(
                            solar_factor
                        )
                    elif is_cloudy_day:
                        cloudy_values_by_field["solar_radiation_wm2"].append(
                            solar_factor
                        )
                if cloud_factor is not None:
                    daily_values_by_field["clouds"].append(cloud_factor)
                    if is_clear_day:
                        clear_values_by_field["clouds"].append(cloud_factor)
                    elif is_cloudy_day:
                        cloudy_values_by_field["clouds"].append(cloud_factor)
                if daily_wind_factor is not None:
                    daily_values_by_field["wind"].append(daily_wind_factor)
                if daily_humidity_factor is not None:
                    daily_values_by_field["humidity"].append(daily_humidity_factor)
                if rain_difference_mm is not None:
                    daily_values_by_field["rain"].append(rain_difference_mm)
                if daily_pressure_offset_hpa is not None:
                    daily_values_by_field["pressure"].append(daily_pressure_offset_hpa)
                sample_days += 1
                if is_clear_day:
                    clear_sample_days += 1
                elif is_cloudy_day:
                    cloudy_sample_days += 1
                for hourly_precision_row in day_hourly_rows:
                    hour = hourly_precision_row[1]
                    if hour is None or not 7 <= hour <= 17:
                        continue
                    hourly_solar_factor = hourly_precision_row[4]
                    hourly_cloud_factor = hourly_precision_row[7]
                    has_measured_solar_sample = bool(hourly_precision_row[23])
                    if has_measured_solar_sample and hourly_solar_factor is not None:
                        if SOLAR_FACTOR_MIN <= hourly_solar_factor <= SOLAR_FACTOR_MAX:
                            if hour not in hourly_solar_factors:
                                hourly_solar_factors[hour] = []
                            hourly_solar_factors[hour].append(hourly_solar_factor)
                    if hourly_cloud_factor is not None:
                        if 0.1 <= hourly_cloud_factor <= 5.0:
                            if hour not in hourly_cloud_factors:
                                hourly_cloud_factors[hour] = []
                            hourly_cloud_factors[hour].append(hourly_cloud_factor)
                continue
            rolling_factors = {}
            confidence_by_field = {}
            offset_fields = ["temperature", "pressure"]
            for rolling_field_name, field_samples in daily_values_by_field.items():
                if field_samples and len(field_samples) >= 3:
                    mean_factor = sum(field_samples) / len(field_samples)
                    rolling_factors[rolling_field_name] = round(mean_factor, 3)
                    sample_size_confidence = min(1.0, len(field_samples) / 7)
                    variance_mean = mean_factor
                    variance = sum(
                        (
                            (sample_value - variance_mean) ** 2
                            for sample_value in field_samples
                        )
                    ) / len(field_samples)
                    std_dev = (math.sqrt if True else math.sqrt)(variance)
                    spread_confidence = max(0.0, 1.0 - std_dev * 2)
                    confidence = (sample_size_confidence + spread_confidence) / 2
                    confidence_by_field[rolling_field_name] = round(confidence, 2)
                else:
                    if rolling_field_name in offset_fields:
                        rolling_factors[rolling_field_name] = 0.0
                    else:
                        rolling_factors[rolling_field_name] = 1.0
                    confidence_by_field[rolling_field_name] = 0.0
            stored_solar_precision_row = await self.fetch_one(
                "SELECT solar_factor, sample_days, confidence\n                   FROM weather_precision_factors\n                   WHERE id = 1"
            )
            (
                solar_factor,
                resolved_solar_sample_days,
                resolved_solar_confidence,
                used_preserved_solar_state,
            ) = resolve_preserved_solar_precision_state(
                rolling_solar_factor=rolling_factors.get("solar_radiation_wm2", 1.0),
                rolling_sample_days=solar_sample_days,
                rolling_confidence=confidence_by_field.get("solar_radiation_wm2", 0.0),
                stored_solar_factor=(
                    None
                    if stored_solar_precision_row is None
                    else stored_solar_precision_row[0]
                ),
                stored_sample_days=(
                    None
                    if stored_solar_precision_row is None
                    else stored_solar_precision_row[1]
                ),
                stored_confidence=(
                    None
                    if stored_solar_precision_row is None
                    else stored_solar_precision_row[2]
                ),
            )
            await self.execute_query(
                "INSERT INTO weather_precision_factors\n                   (id, temperature_factor, solar_factor, cloud_factor,\n                    wind_factor, humidity_factor, rain_factor, pressure_factor,\n                    sample_days, confidence, updated_at)\n                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n                   ON CONFLICT(id) DO UPDATE SET\n                       temperature_factor = excluded.temperature_factor,\n                       solar_factor = excluded.solar_factor,\n                       cloud_factor = excluded.cloud_factor,\n                       wind_factor = excluded.wind_factor,\n                       humidity_factor = excluded.humidity_factor,\n                       rain_factor = excluded.rain_factor,\n                       pressure_factor = excluded.pressure_factor,\n                       sample_days = excluded.sample_days,\n                       confidence = excluded.confidence,\n                       updated_at = CURRENT_TIMESTAMP",
                (
                    rolling_factors.get("temperature", 0.0),
                    solar_factor,
                    rolling_factors.get("clouds", 1.0),
                    rolling_factors.get("wind", 1.0),
                    rolling_factors.get("humidity", 1.0),
                    1.0,
                    rolling_factors.get("pressure", 0.0),
                    resolved_solar_sample_days,
                    resolved_solar_confidence,
                ),
            )
            if used_preserved_solar_state:
                _LOGGER.info(
                    "Preserved stored GHI solar_factor=%.3f sample_days=%d confidence=%.2f until rolling window is apply-ready (rolling_days=%d rolling_confidence=%.2f)",
                    solar_factor,
                    resolved_solar_sample_days,
                    resolved_solar_confidence,
                    solar_sample_days,
                    confidence_by_field.get("solar_radiation_wm2", 0.0),
                )
            solar_hourly_values_to_save = hourly_solar_factors
            if not solar_precision_window_is_apply_ready(
                solar_sample_days, confidence_by_field.get("solar_radiation_wm2", 0.0)
            ):
                solar_hourly_values_to_save = {}
            await self._save_hourly_factors(
                solar_hourly_values_to_save, hourly_cloud_factors, hourly_min_samples
            )
            await self._save_weather_specific_factors(
                clear_values_by_field,
                cloudy_values_by_field,
                clear_sample_days,
                cloudy_sample_days,
                weather_min_samples,
            )
            _LOGGER.info(
                "Updated rolling factors: %d weather days/%d measured-GHI days (clear=%d, cloudy=%d), solar=%.3f, hourly_hours=%d",
                sample_days,
                solar_sample_days,
                clear_sample_days,
                cloudy_sample_days,
                solar_factor,
                len(solar_hourly_values_to_save),
            )
        except Exception as error:
            _LOGGER.error("Error updating rolling averages: %s", error, exc_info=True)
        if True:
            return

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
            for hour, hourly_samples in hourly_solar.items():
                if len(hourly_samples) >= min_samples:
                    mean_factor = sum(hourly_samples) / len(hourly_samples)
                    sample_size_confidence = min(1.0, len(hourly_samples) / 7)
                    variance = sum(
                        (
                            (sample_value - mean_factor) ** 2
                            for sample_value in hourly_samples
                        )
                    ) / len(hourly_samples)
                    std_dev = (math_module.sqrt if True else math_module.sqrt)(variance)
                    spread_confidence = max(0.0, 1.0 - std_dev * 0.5)
                    confidence = round(
                        (sample_size_confidence + spread_confidence) / 2, 2
                    )
                    await self.execute_query(
                        "INSERT INTO weather_precision_hourly_factors\n                           (hour, factor_type, factor_value, sample_count, confidence, std_dev)\n                           VALUES (?, 'solar_radiation_wm2', ?, ?, ?, ?)\n                           ON CONFLICT(hour, factor_type) DO UPDATE SET\n                               factor_value = excluded.factor_value,\n                               sample_count = excluded.sample_count,\n                               confidence = excluded.confidence,\n                               std_dev = excluded.std_dev,\n                               updated_at = CURRENT_TIMESTAMP",
                        (
                            hour,
                            round(mean_factor, 3),
                            len(hourly_samples),
                            confidence,
                            round(std_dev, 3),
                        ),
                    )
            for hour, hourly_samples in hourly_cloud.items():
                if len(hourly_samples) >= min_samples:
                    mean_factor = sum(hourly_samples) / len(hourly_samples)
                    sample_size_confidence = min(1.0, len(hourly_samples) / 7)
                    variance = sum(
                        (
                            (sample_value - mean_factor) ** 2
                            for sample_value in hourly_samples
                        )
                    ) / len(hourly_samples)
                    std_dev = (math_module.sqrt if True else math_module.sqrt)(variance)
                    spread_confidence = max(0.0, 1.0 - std_dev * 0.5)
                    confidence = round(
                        (sample_size_confidence + spread_confidence) / 2, 2
                    )
                    await self.execute_query(
                        "INSERT INTO weather_precision_hourly_factors\n                           (hour, factor_type, factor_value, sample_count, confidence, std_dev)\n                           VALUES (?, 'clouds', ?, ?, ?, ?)\n                           ON CONFLICT(hour, factor_type) DO UPDATE SET\n                               factor_value = excluded.factor_value,\n                               sample_count = excluded.sample_count,\n                               confidence = excluded.confidence,\n                               std_dev = excluded.std_dev,\n                               updated_at = CURRENT_TIMESTAMP",
                        (
                            hour,
                            round(mean_factor, 3),
                            len(hourly_samples),
                            confidence,
                            round(std_dev, 3),
                        ),
                    )
        except Exception as error:
            _LOGGER.error("Error saving hourly factors: %s", error)
        if True:
            return

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
            for comparison_field_name, field_samples in clear_values.items():
                if len(field_samples) >= min_samples:
                    mean_factor = sum(field_samples) / len(field_samples)
                    confidence = min(1.0, len(field_samples) / 7)
                    await self.execute_query(
                        "INSERT INTO weather_precision_weather_specific\n                           (weather_type, factor_type, factor_value, sample_days, confidence)\n                           VALUES ('clear', ?, ?, ?, ?)\n                           ON CONFLICT(weather_type, factor_type) DO UPDATE SET\n                               factor_value = excluded.factor_value,\n                               sample_days = excluded.sample_days,\n                               confidence = excluded.confidence,\n                               updated_at = CURRENT_TIMESTAMP",
                        (
                            comparison_field_name,
                            round(mean_factor, 3),
                            clear_days,
                            round(confidence, 2),
                        ),
                    )
            for comparison_field_name, field_samples in cloudy_values.items():
                if len(field_samples) >= min_samples:
                    mean_factor = sum(field_samples) / len(field_samples)
                    confidence = min(1.0, len(field_samples) / 7)
                    await self.execute_query(
                        "INSERT INTO weather_precision_weather_specific\n                           (weather_type, factor_type, factor_value, sample_days, confidence)\n                           VALUES ('cloudy', ?, ?, ?, ?)\n                           ON CONFLICT(weather_type, factor_type) DO UPDATE SET\n                               factor_value = excluded.factor_value,\n                               sample_days = excluded.sample_days,\n                               confidence = excluded.confidence,\n                               updated_at = CURRENT_TIMESTAMP",
                        (
                            comparison_field_name,
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
            hourly_factor_rows = await self.fetch_all(
                "SELECT hour, factor_type, factor_value, sample_count, confidence, std_dev\n                   FROM weather_precision_hourly_factors\n                   WHERE factor_type IN ('solar_radiation_wm2', 'clouds')"
            )
            hourly_factors = {"solar_radiation_wm2": {}, "clouds": {}, "min_samples": 3}
            for hourly_factor_row in hourly_factor_rows:
                hour, factor_type, factor_value, sample_count, confidence, std_dev = (
                    hourly_factor_row
                )
                if factor_type in hourly_factors:
                    hourly_factors[factor_type][str(hour)] = {
                        "factor": factor_value,
                        "samples": sample_count,
                        "confidence": confidence or 0.0,
                        "std_dev": std_dev,
                    }
            return hourly_factors
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
        field_factors = hourly_factors.get(field, {})
        hour_factor = field_factors.get(str(hour))
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
            weather_factor_rows = await self.fetch_all(
                "SELECT factor_type, factor_value, confidence\n                   FROM weather_precision_weather_specific\n                   WHERE weather_type = ?",
                (weather_type,),
            )
            if not weather_factor_rows:
                return await self.get_correction_factors()
            weather_factors = {
                "temperature": 0.0,
                "solar_radiation_wm2": 1.0,
                "clouds": 1.0,
                "wind": 1.0,
                "humidity": 1.0,
                "rain": 1.0,
                "pressure": 0.0,
            }
            for weather_factor_row in weather_factor_rows:
                factor_type, factor_value, confidence = weather_factor_row
                if (
                    factor_type in weather_factors
                    and confidence
                    and (confidence >= 0.3)
                ):
                    weather_factors[factor_type] = factor_value
            return weather_factors
        except Exception as error:
            _LOGGER.error("Error getting weather-specific factors: %s", error)
            return await self.get_correction_factors()
