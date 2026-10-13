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

    async def _calculate_hour_consumption(
        self,
        hour_data: Dict[str, Any],
        hour_dt: datetime,
        sink_kwargs: Optional[Dict[str, Any]] = None,
        indoor_temp: float = DEFAULT_INDOOR_TEMP,
        suppress_space_heating: bool = False,
        day_factor: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Hybrid Thermodynamics + ML consumption prediction for one hour. @zara"""
        try:
            _var_var_20 = hour_dt.hour
            _var_var_73 = float(hour_data.get("temperature", 5))
            _var_var_74 = float(hour_data.get("wind_speed", 3))
            _var_var_75 = float(hour_data.get("humidity", 70))
            _var_var_76 = float(
                hour_data.get("cloud_cover", hour_data.get("clouds", 50))
            )
            _var_var_77 = self._calculate_thermodynamics_consumption(
                hour_data,
                hour_dt,
                sink_kwargs=sink_kwargs,
                indoor_temp=indoor_temp,
                suppress_space_heating=suppress_space_heating,
                day_factor=day_factor,
            )
            _var_var_78 = 0.0
            _var_var_79 = 0.0
            _var_var_80 = False
            _var_var_81 = 0.0
            _var_var_82 = 0.0
            _var_var_83 = False
            _var_var_84 = False
            if self._use_ai and self.ai_predictor:
                _var_var_85 = self.ai_predictor.current_accuracy or 0.0
                _var_var_80 = self.ai_predictor.is_bootstrap_phase()
                if _var_var_80:
                    _var_var_86 = self.ai_predictor.training_samples or 10
                    _var_var_79 = self._calculate_bootstrap_confidence(
                        _var_var_85, _var_var_86, _var_var_73
                    )
                else:
                    _var_var_79 = self._calculate_hourly_ai_confidence(
                        _var_var_85, _var_var_73, _var_var_20
                    )
                _var_var_68 = await self._get_learned_confidence_adjustment(
                    _var_var_76, _var_var_20
                )
                _var_var_79 = _var_var_79 * _var_var_68["advantage_factor"]
                if (
                    _var_var_68["physics_mae"]
                    and _var_var_68["ai_mae"]
                    and (_var_var_68["sample_count"] >= 10)
                ):
                    _var_var_87 = max(0.001, _var_var_68["physics_mae"])
                    _var_var_88 = max(0.001, _var_var_68["ai_mae"])
                    _var_var_89 = 1.0 / _var_var_87 + 1.0 / _var_var_88
                    _var_var_90 = 1.0 / _var_var_88 / _var_var_89
                    _var_var_79 = 0.5 * _var_var_79 + 0.5 * _var_var_90
                _var_var_79 = min(0.96, max(0.0, _var_var_79))
                if _var_var_79 > 0:
                    _var_var_91 = {
                        "temperature": _var_var_73,
                        "wind": _var_var_74,
                        "humidity": _var_var_75,
                        "clouds": _var_var_76,
                        "rain": hour_data.get("precipitation", 0),
                        "pressure": hour_data.get("pressure", 1013.0),
                        "solar_radiation_wm2": hour_data.get("solar_radiation", 0),
                        "dni": hour_data.get("direct_radiation", 0),
                        "diffuse_radiation": hour_data.get("diffuse_radiation", 0),
                    }
                    try:
                        _var_var_92 = await self.ai_predictor.predict_hour_ensemble(
                            hour=_var_var_20, weather_data=_var_var_91
                        )
                        _var_var_81 = _var_var_92.get("lstm_total", 0.0)
                        _var_var_82 = _var_var_92.get("ridge_total", 0.0)
                        _var_var_83 = _var_var_81 > 0
                        _var_var_84 = _var_var_82 > 0
                        if _var_var_83 and _var_var_84:
                            _var_var_78 = (
                                _var_var_81 * ENSEMBLE_LSTM_WEIGHT
                                + _var_var_82 * ENSEMBLE_RIDGE_WEIGHT
                            )
                        elif _var_var_83:
                            _var_var_78 = _var_var_81
                        elif _var_var_84:
                            _var_var_78 = _var_var_82
                    except AttributeError:
                        try:
                            _var_var_92 = (
                                await self.ai_predictor.predict_hour_per_group_ensemble(
                                    hour=_var_var_20,
                                    weather_data=_var_var_91,
                                    astronomy_data={},
                                    target_datetime=hour_dt,
                                )
                            )
                            _var_var_81 = _var_var_92.get("lstm_total", 0.0)
                            _var_var_82 = _var_var_92.get("ridge_total", 0.0)
                            _var_var_83 = _var_var_81 > 0
                            _var_var_84 = _var_var_82 > 0
                            if _var_var_83 and _var_var_84:
                                _var_var_78 = (
                                    _var_var_81 * ENSEMBLE_LSTM_WEIGHT
                                    + _var_var_82 * ENSEMBLE_RIDGE_WEIGHT
                                )
                            elif _var_var_83:
                                _var_var_78 = _var_var_81
                            elif _var_var_84:
                                _var_var_78 = _var_var_82
                        except Exception:
                            pass
                    if _var_var_83 and _var_var_84:
                        _var_var_93 = (_var_var_81 + _var_var_82) / 2.0
                        if _var_var_93 > 0.005:
                            _var_var_94 = abs(_var_var_81 - _var_var_82) / _var_var_93
                        else:
                            _var_var_94 = 0.0
                        if _var_var_94 > DISAGREEMENT_THRESHOLD:
                            _var_var_95 = max(0.5, 1.0 - _var_var_94 * 0.5)
                            _var_var_79 *= _var_var_95
            _var_var_96 = False
            _var_var_97 = _var_var_79
            _var_var_37, _var_var_98, _var_var_69 = self._blend_calibrator_state(
                _var_var_73, _var_var_74, _var_var_20
            )
            if (
                _var_var_79 > 0
                and _var_var_78 > 0
                and (_var_var_77 >= 0.05)
                and (_var_var_37 >= 3)
            ):
                _var_var_99 = abs(_var_var_78 - _var_var_77) / _var_var_77
                _var_var_100 = self._ai_follows_correction(
                    _var_var_77, _var_var_78, _var_var_69, _var_var_98
                )
                _var_var_101 = _var_var_78 > _var_var_77
                if _var_var_101:
                    _var_var_102 = self.heating_capacity_kw / 25.0
                    _var_var_103 = _var_var_102
                    if self._thermodynamics_engine is not None:
                        _var_var_103 = self._thermodynamics_engine.u_factor
                    _var_var_104 = _var_var_103 > _var_var_102 * 1.05
                    _var_var_105 = True
                    if _var_var_83 and _var_var_84:
                        _var_var_106 = (_var_var_81 + _var_var_82) / 2.0
                        if _var_var_106 > 0.005:
                            _var_var_105 = (
                                abs(_var_var_81 - _var_var_82) / _var_var_106
                                <= RIDGE_CONFIRMS_THRESHOLD
                            )
                    if _var_var_100 is True and _var_var_99 <= 1.0:
                        _var_var_96 = True
                        if _var_var_99 <= MAX_AI_THERMO_DEVIATION:
                            _var_var_97 = max(_var_var_79, 0.9)
                        else:
                            _var_var_107 = _var_var_99 - MAX_AI_THERMO_DEVIATION
                            _var_var_108 = max(0.2, 1.0 - _var_var_107 * 0.6)
                            _var_var_97 = _var_var_79 * _var_var_108
                    elif (
                        _var_var_104
                        and _var_var_105
                        and (_var_var_99 <= MAX_AI_THERMO_DEVIATION)
                    ):
                        _var_var_96 = True
                        _var_var_97 = max(_var_var_79, 0.9)
                elif _var_var_99 <= MAX_AI_THERMO_DEVIATION or _var_var_100 is True:
                    _var_var_96 = True
                    if _var_var_99 <= MAX_AI_THERMO_DEVIATION:
                        _var_var_97 = max(_var_var_79, 0.9)
                    else:
                        _var_var_107 = _var_var_99 - MAX_AI_THERMO_DEVIATION
                        _var_var_108 = max(0.2, 1.0 - _var_var_107 * 0.6)
                        _var_var_97 = _var_var_79 * _var_var_108
            if _var_var_96:
                _var_var_109 = (
                    _var_var_77 * (1 - _var_var_97) + _var_var_78 * _var_var_97
                )
                _var_var_110 = "ridge" if _var_var_80 else "lstm"
                _var_var_36 = f"thermo_{_var_var_110}{int(_var_var_97 * 100)}"
            else:
                _var_var_109 = _var_var_77
                _var_var_36 = "thermodynamics"
            return (
                (
                    (
                        {
                            "consumption": max(
                                0.0, min(_var_var_109, self.heating_capacity_kw * 1.1)
                            ),
                            "thermo_kwh": round(_var_var_77, 4),
                            "ai_kwh": (
                                round(_var_var_78, 4)
                                if _var_var_78 > 0 and _var_var_77 > 0
                                else None
                            ),
                            "ai_confidence": (
                                round(_var_var_97, 4) if _var_var_97 > 0 else None
                            ),
                            "method": _var_var_36,
                            "ml_contribution": (
                                int(_var_var_97 * 100) if _var_var_96 else 0
                            ),
                            "lstm_kwh": round(_var_var_81, 4) if _var_var_83 else None,
                            "ridge_kwh": round(_var_var_82, 4) if _var_var_84 else None,
                        }
                        if True
                        else None
                    )
                    if True
                    else None
                )
                if True
                else None
            )
        except Exception as _var_var_28:
            _LOGGER.debug(f"Hour consumption calc failed: {_var_var_28}")
            _var_var_111 = self._calculate_thermodynamics_consumption(
                hour_data,
                hour_dt,
                sink_kwargs=sink_kwargs,
                indoor_temp=indoor_temp,
                suppress_space_heating=suppress_space_heating,
                day_factor=day_factor,
            )
            return {
                "consumption": _var_var_111,
                "thermo_kwh": round(_var_var_111, 4),
                "ai_kwh": None,
                "ai_confidence": None,
                "method": "thermodynamics",
                "ml_contribution": 0,
                "lstm_kwh": None,
                "ridge_kwh": None,
            }

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
