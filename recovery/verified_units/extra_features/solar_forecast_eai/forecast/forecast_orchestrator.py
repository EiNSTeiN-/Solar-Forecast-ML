# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Forecast Orchestrator Module.
Selects and executes the most appropriate forecast strategy for
heat pump energy consumption prediction.

@zara
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from homeassistant.core import HomeAssistant
from ..const import HEATING_SEASON_MONTHS, SUPPORTED_WP_TYPES
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..data.db_manager import DatabaseManager
from .forecast_rule_based_strategy import RuleBasedForecastStrategy
from .forecast_strategy_base import ForecastResult
from .forecast_weather_calculator import WeatherCalculator

_LOGGER = logging.getLogger(__name__)


class ForecastOrchestrator:
    """Selects and executes the forecast strategy for heat pump consumption. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        data_manager: Any,
        solar_capacity: float,
        weather_calculator: WeatherCalculator,
        db_manager: DatabaseManager,
        wp_type: str,
        cop_rated: float,
        config_entry: Optional[Any] = None,
    ):
        "Initialize the ForecastOrchestrator. @zara\n\nArgs:\n    hass: Home Assistant instance\n    data_manager: Data manager reference\n    solar_capacity: Heating capacity in kW (parameter name kept for API compat)\n    weather_calculator: Weather calculation utilities\n    db_manager: Database manager instance\n"
        self.hass = hass
        self.data_manager = data_manager
        self.heating_capacity_kw = solar_capacity
        self.weather_calculator = weather_calculator
        self._db = db_manager
        if wp_type not in SUPPORTED_WP_TYPES:
            raise ValueError(f"Unsupported heat pump type: {wp_type}")
        if not 1.0 <= float(cop_rated) <= 10.0:
            raise ValueError("Rated COP must be between 1.0 and 10.0")
        self.wp_type = wp_type
        self.cop_rated = float(cop_rated)
        self.config_entry = config_entry
        self.rule_based_strategy = None
        self._ai_predictor = None
        self._historical_cache = {}
        self.active_strategy_name = None
        _LOGGER.debug(
            "ForecastOrchestrator initialized (heating_capacity=%.1f kW)",
            self.heating_capacity_kw,
        )

    def initialize_strategies(
        self, ai_predictor: Optional[Any] = None, error_handler: Optional[Any] = None
    ) -> None:
        """Initialize the forecast strategy with AI. @zara"""
        _LOGGER.info("Initializing forecast strategies...")
        self._ai_predictor = ai_predictor
        if self._ai_predictor:
            self._historical_cache = self._ai_predictor._historical_cache
        try:
            self.rule_based_strategy = RuleBasedForecastStrategy(
                weather_calculator=self.weather_calculator,
                heating_capacity_kw=self.heating_capacity_kw,
                db_manager=self._db,
                orchestrator=self,
                ai_predictor=ai_predictor,
                wp_type=self.wp_type,
                cop_rated=self.cop_rated,
            )
            if ai_predictor and ai_predictor.is_ready():
                _LOGGER.info("Forecast strategy initialized with local AI")
            else:
                _LOGGER.info("Forecast strategy initialized (thermodynamics fallback)")
        except Exception as error:
            _LOGGER.error(
                f"Failed to initialize forecast strategy: {error}", exc_info=True
            )
            self.rule_based_strategy = None
        return None if True else None

    async def orchestrate_forecast(
        self,
        current_weather: Optional[Dict[str, Any]] = None,
        hourly_forecast: Optional[List[Dict[str, Any]]] = None,
        external_sensors: Optional[Dict[str, Any]] = None,
        historical_avg: Optional[float] = None,
        ml_prediction_today: Optional[float] = None,
        ml_prediction_tomorrow: Optional[float] = None,
        correction_factor: float = 1.0,
    ) -> Dict[str, Any]:
        """Orchestrate consumption forecast creation with all available data. @zara"""
        normalized_hourly_weather = hourly_forecast if hourly_forecast else []
        normalized_sensors = external_sensors if external_sensors else {}
        return await self.create_forecast(
            hourly_weather_forecast=normalized_hourly_weather,
            sensor_data=normalized_sensors,
            correction_factor=correction_factor,
        )

    async def create_forecast(
        self,
        hourly_weather_forecast: List[Dict[str, Any]],
        sensor_data: Dict[str, Any],
        correction_factor: float = 1.0,
    ) -> Dict[str, Any]:
        """Create daily consumption forecast using AI with thermodynamics fallback. @zara"""
        _LOGGER.debug("Creating consumption forecast (AI + Thermodynamics)...")
        result = None
        lag_features = {}
        try:
            current_time = dt_util.now()
            yesterday = current_time - timedelta(days=1)
            yesterday_str = yesterday.date().isoformat()
            database_row = await self._db.fetchone(
                "SELECT SUM(actual_kwh)\n                   FROM hourly_predictions\n                   WHERE target_date = ?",
                (yesterday_str,),
            )
            yesterday_actual_kwh = (
                database_row[0] if database_row and database_row[0] else 0.0
            )
            lag_features["consumption_yesterday"] = float(yesterday_actual_kwh)
            _LOGGER.debug(
                "Lag-Feature 'consumption_yesterday' = %.2f kWh (from DB: %s)",
                yesterday_actual_kwh,
                yesterday_str,
            )
        except Exception as error:
            _LOGGER.warning(
                f"Could not retrieve lag feature 'consumption_yesterday': {error}"
            )
            lag_features["consumption_yesterday"] = 0.0
        if self.rule_based_strategy and self.rule_based_strategy.is_available():
            try:
                result = await self.rule_based_strategy.calculate_forecast(
                    hourly_weather_forecast=hourly_weather_forecast,
                    sensor_data=sensor_data,
                    correction_factor=correction_factor,
                    lag_features=lag_features,
                )
                _LOGGER.info(
                    "Consumption forecast: Today=%.2f kWh, Tomorrow=%.2f kWh, Day After=%.2f kWh, Method=%s",
                    result.forecast_today,
                    result.forecast_tomorrow,
                    result.forecast_day_after_tomorrow,
                    result.method,
                )
                await self._store_daily_forecasts(result)
            except Exception as lag_error:
                _LOGGER.error(
                    f"Forecast calculation failed: {lag_error}", exc_info=True
                )
                result = None
        else:
            _LOGGER.error("Forecast strategy not available")
            result = None
        if not result:
            _LOGGER.critical("Emergency Fallback: Forecast failed. Returning zeros.")
            return {
                "today": 0.0,
                "tomorrow": 0.0,
                "day_after_tomorrow": 0.0,
                "peak_time": "06:00",
                "confidence": 0.0,
                "method": "fallback_empty",
                "model_accuracy": None,
                "best_hour": None,
                "best_hour_kwh": None,
                "hourly": [],
                "today_raw": None,
                "safeguard_applied": False,
            }
        best_hour = result.best_hour_today
        best_hour_consumption_kwh = result.best_hour_consumption_kwh
        hourly_values = result.hourly_values or []
        return {
            "today": round(result.forecast_today, 2),
            "tomorrow": round(result.forecast_tomorrow, 2),
            "day_after_tomorrow": round(result.forecast_day_after_tomorrow, 2),
            "peak_time": f"{best_hour:02d}:00" if best_hour is not None else "06:00",
            "confidence": round(result.confidence_today, 1),
            "method": result.method,
            "model_accuracy": result.model_accuracy,
            "best_hour": best_hour,
            "best_hour_kwh": (
                round(best_hour_consumption_kwh, 3)
                if best_hour_consumption_kwh is not None
                else None
            ),
            "hourly": hourly_values,
            "today_raw": (
                round(result.forecast_today_raw, 2)
                if result.forecast_today_raw is not None
                else None
            ),
            "safeguard_applied": result.safeguard_applied_today,
        }

    async def _store_daily_forecasts(self, result: ForecastResult) -> None:
        """Store daily forecasts in database. Respects morning routine locks. @zara"""
        try:
            created_at = dt_util.now()
            today = created_at.date()
            tomorrow = today + timedelta(days=1)
            day_after_tomorrow = today + timedelta(days=2)
            daily_forecasts = [
                (
                    "today",
                    today,
                    result.forecast_today,
                    result.forecast_today_raw,
                    result.safeguard_applied_today,
                ),
                (
                    "tomorrow",
                    tomorrow,
                    result.forecast_tomorrow,
                    result.forecast_tomorrow_raw,
                    result.safeguard_applied_tomorrow,
                ),
                (
                    "day_after_tomorrow",
                    day_after_tomorrow,
                    result.forecast_day_after_tomorrow,
                    result.forecast_day_after_raw,
                    result.safeguard_applied_day_after,
                ),
            ]
            for (
                forecast_type,
                forecast_date,
                forecast_kwh,
                raw_forecast_kwh,
                safeguard_applied,
            ) in daily_forecasts:
                existing_row = await self._db.fetchone(
                    "SELECT locked FROM daily_forecasts\n                       WHERE forecast_type = ? AND forecast_date = ?",
                    (forecast_type, forecast_date.isoformat()),
                )
                if existing_row and existing_row[0]:
                    _LOGGER.debug(
                        "Skipping %s forecast storage - locked by morning routine",
                        forecast_type,
                    )
                    continue
                await self._db.execute(
                    "INSERT INTO daily_forecasts\n                       (forecast_type, forecast_date, prediction_kwh, prediction_kwh_raw,\n                        safeguard_applied, source, created_at)\n                       VALUES (?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                           prediction_kwh = excluded.prediction_kwh,\n                           prediction_kwh_raw = excluded.prediction_kwh_raw,\n                           safeguard_applied = excluded.safeguard_applied,\n                           source = excluded.source\n                       WHERE locked = 0 OR locked IS NULL",
                    (
                        forecast_type,
                        forecast_date.isoformat(),
                        forecast_kwh,
                        raw_forecast_kwh,
                        safeguard_applied,
                        result.method,
                        dt_util.now(),
                    ),
                )
                await self._db.execute(
                    "INSERT INTO daily_forecast_updates\n                       (forecast_type, forecast_date, prediction_kwh, source, updated_at)\n                       VALUES (?, ?, ?, ?, ?)",
                    (
                        forecast_type,
                        forecast_date.isoformat(),
                        forecast_kwh,
                        result.method,
                        dt_util.now(),
                    ),
                )
                continue
        except Exception as error:
            _LOGGER.debug(f"Could not store daily forecasts: {error}")
        return None if True else None

    def calculate_next_hour_prediction(
        self,
        forecast_today_kwh: float,
        weather_data: Optional[Dict[str, Any]] = None,
        sensor_data: Optional[Dict[str, Any]] = None,
    ) -> float:
        """Estimate heat pump consumption for the next full hour. @zara

        Uses temperature-based heating demand profile instead of solar curve.
        Heat pumps consume more at colder temperatures and during night hours.
        """
        _LOGGER.debug("Calculating next hour consumption prediction...")
        try:
            current_time = dt_util.now()
            next_hour = current_time + timedelta(hours=1)
            next_hour_index = next_hour.hour
            temperature_c = None
            if weather_data:
                temperature_c = weather_data.get("temperature")
            if temperature_c is None and sensor_data:
                temperature_c = sensor_data.get("temperature")
            base_consumption_kwh = self._get_hourly_consumption_base(
                forecast_today_kwh, next_hour_index, temperature_c
            )
            if base_consumption_kwh is not None:
                starting_consumption_kwh = base_consumption_kwh
            else:
                starting_consumption_kwh = forecast_today_kwh / 24.0
            adjustment_factors = self._get_realtime_adjustment_factors(
                weather_data, sensor_data
            )
            adjusted_consumption_kwh = starting_consumption_kwh
            applied_factors = []
            for factor_name, factor_value in adjustment_factors.items():
                adjusted_consumption_kwh *= factor_value
                applied_factors.append(f"{factor_name}={factor_value:.2f}")
            capacity_limit_kw = self.heating_capacity_kw * 1.2
            adjusted_consumption_kwh = min(adjusted_consumption_kwh, capacity_limit_kw)
            next_hour_consumption_kwh = max(0.0, adjusted_consumption_kwh)
            _LOGGER.info(
                "Next Hour (%02d:00) Consumption: Base=%.3f kWh * Adjustments [%s] -> Final=%.3f kWh",
                next_hour_index,
                starting_consumption_kwh,
                ", ".join(applied_factors),
                next_hour_consumption_kwh,
            )
            return round(next_hour_consumption_kwh, 3)
        except Exception as error:
            _LOGGER.error(f"Next hour prediction failed: {error}", exc_info=True)
            return 0.0

    def _get_hourly_consumption_base(
        self,
        forecast_today_kwh: float,
        target_hour: int,
        outdoor_temp: Optional[float] = None,
    ) -> Optional[float]:
        """Calculate base consumption for a specific hour using heating demand profile. @zara

        Heat pump consumption profile (opposite of solar):
        - Night hours (22-06): Higher consumption (heating demand peaks)
        - Morning (06-09): Medium-high (morning warm-up)
        - Daytime (09-16): Lower consumption (solar gains, mild temps)
        - Evening (16-22): Medium consumption (cooling outside)
        """
        hourly_weights = {
            0: 1.2,
            1: 1.3,
            2: 1.3,
            3: 1.4,
            4: 1.4,
            5: 1.3,
            6: 1.2,
            7: 1.1,
            8: 1.0,
            9: 0.8,
            10: 0.6,
            11: 0.5,
            12: 0.4,
            13: 0.4,
            14: 0.5,
            15: 0.6,
            16: 0.7,
            17: 0.8,
            18: 0.9,
            19: 1.0,
            20: 1.1,
            21: 1.1,
            22: 1.2,
            23: 1.2,
        }
        if outdoor_temp is not None:
            if outdoor_temp > 15.0:
                hourly_weights = {
                    hour: 0.6 + 0.4 * (1.0 if 6 <= hour <= 8 else 0.3)
                    for hour in range(24)
                }
        weight_sum = sum(hourly_weights.values())
        if weight_sum <= 0:
            return
        hour_share = hourly_weights.get(target_hour, 1.0) / weight_sum
        base_consumption_kwh = forecast_today_kwh * hour_share
        _LOGGER.debug(
            "Hourly consumption base: Hour=%d, Fraction=%.4f -> Base=%.3f kWh",
            target_hour,
            hour_share,
            base_consumption_kwh,
        )
        return base_consumption_kwh

    def _get_realtime_adjustment_factors(
        self,
        current_weather_data: Optional[Dict[str, Any]],
        current_sensor_data: Optional[Dict[str, Any]],
    ) -> Dict[str, float]:
        """Calculate adjustment multipliers based on current conditions. @zara

        For heat pumps:
        - Temperature: Lower temp -> higher consumption (COP drops)
        - Wind: Higher wind -> higher consumption (wind chill on air-source)
        """
        factors = {"temperature": 1.0, "wind": 1.0}
        observed_temperature_c = None
        if current_sensor_data:
            observed_temperature_c = current_sensor_data.get("temperature")
        if observed_temperature_c is None and current_weather_data:
            observed_temperature_c = current_weather_data.get("temperature")
        if observed_temperature_c is not None:
            if observed_temperature_c < -10:
                factors["temperature"] = 1.4
            elif observed_temperature_c < 0:
                factors["temperature"] = 1.2
            elif observed_temperature_c < 5:
                factors["temperature"] = 1.1
            elif observed_temperature_c < 15:
                factors["temperature"] = 1.0
            else:
                factors["temperature"] = 0.5
        wind_speed_ms = None
        if current_sensor_data:
            wind_speed_ms = current_sensor_data.get("wind_speed")
        if wind_speed_ms is None and current_weather_data:
            wind_speed_ms = current_weather_data.get("wind_speed")
        if wind_speed_ms is not None and wind_speed_ms > 3.0:
            if wind_speed_ms > 15:
                factors["wind"] = 1.15
            elif wind_speed_ms > 8:
                factors["wind"] = 1.1
            else:
                factors["wind"] = 1.05
        for factor_name in factors:
            factors[factor_name] = max(0.1, min(2.0, factors[factor_name]))
        _LOGGER.debug(f"Real-time adjustment factors: {factors}")
        return factors

    async def get_latest_forecasts(self) -> Dict[str, Any]:
        """Get latest forecasts from database. @zara"""
        try:
            today = dt_util.now().date().isoformat()
            forecast_rows = await self._db.fetchall(
                "SELECT forecast_type, forecast_date, prediction_kwh, locked, source\n                   FROM daily_forecasts\n                   WHERE forecast_date >= ?\n                   ORDER BY forecast_date",
                (today,),
            )
            result = {"today": None, "tomorrow": None, "day_after_tomorrow": None}
            for database_row in forecast_rows:
                forecast_type = database_row[0]
                if forecast_type in result:
                    result[forecast_type] = {
                        "date": str(database_row[1]),
                        "prediction_kwh": database_row[2],
                        "locked": database_row[3],
                        "source": database_row[4],
                    }
            return result
        except Exception as error:
            _LOGGER.error(f"Could not get latest forecasts: {error}")
            return {"today": None, "tomorrow": None, "day_after_tomorrow": None}
