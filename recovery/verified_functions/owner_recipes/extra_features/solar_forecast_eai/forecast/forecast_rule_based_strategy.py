# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from datetime import date, datetime, timedelta, timezone, tzinfo
from math import isfinite
from typing import Any, Dict, List, Optional, Tuple
from homeassistant.util import dt as dt_util
from ..const import (
    CONF_FLOW_TEMP_ENTITY,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_OPERATION_MODE_ENTITY,
    CONF_TARGET_TEMP_ENTITY,
    DEFAULT_COP_RATED,
    DEFAULT_WP_TYPE,
    DOMAIN,
    SUPPORTED_WP_TYPES,
)
from ..core.core_helpers import SafeDateTimeUtil as dt_util_safe, get_season
from ..data.db_manager import DatabaseManager
from ..setup_state import cop_sink_kwargs, dhw_forecast_kwargs
from ..thermodynamics import (
    ThermodynamicsCalibrator,
    ThermodynamicsEngine,
    WeatherConditions,
)
from ..thermodynamics.thermodynamics_engine import (
    DEFAULT_INDOOR_TEMP,
    local_calendar_date,
    space_heating_suppressed_by_day,
)
from .forecast_strategy_base import ForecastResult, ForecastStrategy
from .forecast_weather_calculator import WeatherCalculator


class RuleBasedForecastStrategy(ForecastStrategy):

    async def calculate_forecast(
        self,
        hourly_weather_forecast: List[Dict[str, Any]],
        sensor_data: Dict[str, Any],
        correction_factor: float,
        **kwargs,
    ) -> ForecastResult:
        """Calculate consumption forecast using thermodynamics engine. @zara"""
        try:
            await self._ensure_components_initialized()
            if not hourly_weather_forecast:
                return self._empty_result()
            _var_var_0 = dt_util_safe.now()
            _var_var_1 = _var_var_0.date()
            _var_var_2 = _var_var_1 + timedelta(days=1)
            _var_var_3 = _var_var_1 + timedelta(days=2)
            try:
                _var_var_4 = float(correction_factor)
            except (TypeError, ValueError):
                _var_var_4 = 1.0
            if not isfinite(_var_var_4) or not 0.5 <= _var_var_4 <= 1.5:
                _LOGGER.warning(
                    "Invalid correction factor %r; using 1.0", correction_factor
                )
                _var_var_4 = 1.0
            _var_var_5 = 0.0
            _var_var_6 = 0.0
            _var_var_7 = 0.0
            _var_var_8 = 0.0
            _var_var_9 = []
            _var_var_10 = None
            _var_var_11 = 0.0
            _var_var_12 = self._cop_sink_kwargs()
            _var_var_13 = self._forecast_timezone()
            _var_var_14 = self._configured_indoor_temp()
            await self._apply_learned_u_factor()
            _var_var_15 = self._blocked_space_heating_days(hourly_weather_forecast)
            _var_var_16 = self._day_correction_factors(hourly_weather_forecast)
            for _var_var_17 in hourly_weather_forecast:
                _var_var_18 = self._parse_datetime(_var_var_17)
                if not _var_var_18:
                    continue
                _var_var_19 = _var_var_18.date()
                _var_var_20 = _var_var_18.hour
                _var_var_21 = _var_var_15.get(
                    local_calendar_date(_var_var_18, _var_var_13), False
                )
                _var_var_22 = local_calendar_date(_var_var_18, _var_var_13)
                _var_var_23 = self._blend_beside_day_factor(
                    _var_var_4, _var_var_16.get(_var_var_22)
                )
                _var_var_24 = await self._calculate_hour_consumption(
                    _var_var_17,
                    _var_var_18,
                    sink_kwargs=_var_var_12,
                    indoor_temp=_var_var_14,
                    suppress_space_heating=_var_var_21,
                    day_factor=_var_var_16.get(
                        local_calendar_date(_var_var_18, _var_var_13)
                    ),
                )
                _var_var_25 = _var_var_24["consumption"]
                _var_var_26 = min(_var_var_25 * _var_var_23, self.heating_capacity_kw)
                if _var_var_19 == _var_var_1:
                    _var_var_8 += _var_var_25
                    _var_var_5 += _var_var_26
                    if _var_var_26 > _var_var_11:
                        _var_var_11 = _var_var_26
                        _var_var_10 = _var_var_20
                elif _var_var_19 == _var_var_2:
                    _var_var_6 += _var_var_26
                elif _var_var_19 == _var_var_3:
                    _var_var_7 += _var_var_26
                _var_var_27 = {
                    "hour": _var_var_20,
                    "datetime": _var_var_18.isoformat(),
                    "date": _var_var_19.isoformat(),
                    "production_kwh": round(_var_var_26, 3),
                    "production_kwh_raw": round(_var_var_25, 3),
                    "correction_factor": _var_var_23,
                    "method": _var_var_24["method"],
                    "physics_kwh": _var_var_24["thermo_kwh"],
                    "ai_kwh": _var_var_24["ai_kwh"],
                    "ai_confidence": _var_var_24["ai_confidence"],
                    "ml_contribution": _var_var_24["ml_contribution"],
                    "confidence": 75.0,
                    "lstm_kwh": _var_var_24.get("lstm_kwh"),
                    "ridge_kwh": _var_var_24.get("ridge_kwh"),
                }
                _var_var_9.append(_var_var_27)
                continue
            await self._store_hourly_predictions(_var_var_9)
            return ForecastResult(
                forecast_today=round(_var_var_5, 2),
                forecast_tomorrow=round(_var_var_6, 2),
                forecast_day_after_tomorrow=round(_var_var_7, 2),
                confidence_today=70.0,
                confidence_tomorrow=60.0,
                confidence_day_after=50.0,
                method="thermodynamics" if not self._use_ai else "AI + Thermodynamics",
                calibrated=bool(self._thermodynamics_calibrator),
                best_hour_today=_var_var_10,
                best_hour_consumption_kwh=round(_var_var_11, 3),
                hourly_values=_var_var_9,
                model_accuracy=None,
                forecast_today_raw=_var_var_8,
                safeguard_applied_today=False,
            )
        except Exception as _var_var_28:
            _LOGGER.error(f"Forecast calculation failed: {_var_var_28}", exc_info=True)
            return self._empty_result()

    def _calculate_thermodynamics_consumption(
        self,
        hour_data: Dict[str, Any],
        hour_dt: datetime,
        sink_kwargs: Optional[Dict[str, Any]] = None,
        indoor_temp: float = DEFAULT_INDOOR_TEMP,
        suppress_space_heating: bool = False,
        day_factor: Optional[float] = None,
    ) -> float:
        """Calculate thermodynamics-based hourly consumption. @zara

        Uses ThermodynamicsEngine for COP and heating demand calculation.
        Falls back to simple temperature-based heuristic if engine unavailable.
        """
        self._hour_is_dhw_only = False
        try:
            _var_var_73 = float(hour_data.get("temperature", 5))
            _var_var_74 = float(hour_data.get("wind_speed", 3))
            _var_var_75 = float(hour_data.get("humidity", 70))
            _var_var_76 = float(
                hour_data.get("cloud_cover", hour_data.get("clouds", 50))
            )
            _var_var_112 = self._dhw_kwargs()
            _var_var_113 = bool(_var_var_112.get("has_dhw"))
            if self._thermodynamics_engine:
                _var_var_114 = WeatherConditions(
                    outdoor_temp=_var_var_73,
                    wind_speed=_var_var_74,
                    humidity=_var_var_75,
                    cloud_cover=_var_var_76,
                    precipitation=float(hour_data.get("precipitation", 0)),
                )
                _var_var_115 = self._thermodynamics_engine.calculate_hourly_consumption(
                    weather=_var_var_114,
                    indoor_temp=indoor_temp,
                    hour=hour_dt.hour,
                    suppress_space_heating=suppress_space_heating,
                    **sink_kwargs or {},
                    **_var_var_112,
                )
                _var_var_116 = _var_var_115.consumption_kwh
                self._hour_is_dhw_only = _var_var_113 and (
                    suppress_space_heating or _var_var_115.heating_demand_kw <= 0.01
                )
                if (
                    day_factor is not None
                    and isfinite(day_factor)
                    and (day_factor > 0.0)
                    and (not self._hour_is_dhw_only)
                ):
                    _var_var_116 *= day_factor
                elif self._thermodynamics_calibrator:
                    try:
                        _var_var_117 = (
                            self._thermodynamics_calibrator.get_correction_factor(
                                outdoor_temp=_var_var_73,
                                wind_speed=_var_var_74,
                                hour=hour_dt.hour,
                                is_dhw_only=self._hour_is_dhw_only,
                            )
                        )
                        if _var_var_117 and _var_var_117.confidence > 0.3:
                            _var_var_116 *= _var_var_117.correction_factor
                    except Exception:
                        pass
                return max(0.0, _var_var_116)
            self._hour_is_dhw_only = _var_var_113 and suppress_space_heating
            return self._simple_temperature_heuristic(
                _var_var_73,
                _var_var_74,
                hour_dt.hour,
                indoor_temp=indoor_temp,
                suppress_space_heating=suppress_space_heating,
            )
        except Exception as _var_var_28:
            _LOGGER.debug(f"Thermodynamics consumption calc failed: {_var_var_28}")
            _var_var_73 = float(hour_data.get("temperature", 5))
            return self._simple_temperature_heuristic(
                _var_var_73,
                3.0,
                hour_dt.hour,
                indoor_temp=indoor_temp,
                suppress_space_heating=suppress_space_heating,
            )
