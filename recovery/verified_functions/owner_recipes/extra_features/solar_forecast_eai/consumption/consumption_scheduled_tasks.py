# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import asyncio
import hashlib
import logging
import math
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_point_in_time,
)
from .consumption_morning_routine import MorningRoutineHandler
from .consumption_adaptive_forecast import AdaptiveForecastEngine
from ..data.data_daily_summaries import DailySummariesHandler
from ..data.eai_consumption_sample_store import EAIConsumptionSampleStore
from ..ai.ai_predictor import MIN_RIDGE_SAMPLES, REASON_RIDGE_WARMUP
from ..services.service_system_report import SystemReportGenerator
from ..setup_state import is_true
from ..const import (
    ADAPTIVE_CHECK_HOUR,
    ADAPTIVE_CHECK_MINUTE,
    CONF_ADAPTIVE_FORECAST_MODE,
    CONF_ELECTRICAL_MEASUREMENT_TOPOLOGY,
    CONF_HAS_HEATING_ELEMENT,
    CONF_HEATING_ELEMENT_ENERGY_TODAY_ENTITY,
    CONF_HUMIDITY_SENSOR,
    CONF_PRESSURE_SENSOR,
    CONF_RAIN_SENSOR,
    CONF_TEMP_SENSOR,
    CONF_WIND_SENSOR,
    DEFAULT_ADAPTIVE_FORECAST_MODE,
)
from ..core.core_helpers import SafeDateTimeUtil as dt_util, get_season
from ..astronomy_provider import AstronomyProviderAdapter


class ScheduledTasksManager:

    async def recover_missing_hourly_actuals(self) -> int:
        """Recover missing hourly actuals using yield deltas from sensor. @zara"""
        _var_var_24 = dt_util.now()
        _var_var_25 = _var_var_24.date().isoformat()
        try:
            _var_var_26 = None
            if self.wp_energy_today_entity_id:
                _var_var_27 = self.hass.states.get(self.wp_energy_today_entity_id)
                if _var_var_27 and _var_var_27.state not in (
                    None,
                    "unknown",
                    "unavailable",
                ):
                    try:
                        _var_var_26 = float(_var_var_27.state)
                    except (ValueError, TypeError):
                        pass
            if _var_var_26 is None:
                _LOGGER.warning(
                    "Cannot recover hourly actuals: yield sensor unavailable"
                )
                return 0
            _var_var_28 = await self.db.fetchall(
                "SELECT target_hour, prediction_kwh, actual_kwh\n                   FROM hourly_predictions\n                   WHERE target_date = ?\n                   ORDER BY target_hour",
                (_var_var_25,),
            )
            if not _var_var_28:
                _LOGGER.warning(
                    "Cannot recover hourly actuals: no predictions for today"
                )
                return 0
            _var_var_29 = await self._get_production_window_from_cache(
                _var_var_24.date()
            )
            if not _var_var_29:
                _LOGGER.warning("Cannot recover hourly actuals: no production window")
                return 0
            _var_var_30, _var_var_31 = _var_var_29
            _var_var_32 = _var_var_30.hour
            _var_var_33 = min(_var_var_24.hour, _var_var_31.hour)
            _var_var_34 = []
            _var_var_35 = 0.0
            for _var_var_36 in _var_var_28:
                _var_var_37 = _var_var_36[0]
                _var_var_38 = _var_var_36[2]
                if _var_var_37 < _var_var_32 or _var_var_37 > _var_var_33:
                    continue
                if _var_var_38 is not None:
                    _var_var_35 = _var_var_38
                elif _var_var_37 < _var_var_24.hour:
                    _var_var_34.append(_var_var_37)
            if not _var_var_34:
                _LOGGER.debug("No missing hours to recover")
                return 0
            _var_var_39 = _var_var_26 - _var_var_35
            if _var_var_39 <= 0:
                _LOGGER.debug(
                    "No production delta to distribute (current=%.3f, last_known=%.3f)",
                    _var_var_26,
                    _var_var_35,
                )
                return 0
            _var_var_40 = {}
            _var_var_41 = 0.0
            for _var_var_36 in _var_var_28:
                _var_var_37 = _var_var_36[0]
                if _var_var_37 in _var_var_34:
                    _var_var_42 = _var_var_36[1] or 1.0
                    _var_var_40[_var_var_37] = max(_var_var_42, 0.01)
                    _var_var_41 += _var_var_40[_var_var_37]
            if _var_var_41 == 0:
                _var_var_41 = len(_var_var_34)
                _var_var_40 = {_var_var_43: 1.0 for _var_var_43 in _var_var_34}
            _var_var_44 = 0
            _var_var_45 = _var_var_35
            _LOGGER.info(
                "Recovering %d missing hours: %s - Total delta: %.3f kWh",
                len(_var_var_34),
                _var_var_34,
                _var_var_39,
            )
            for _var_var_37 in sorted(_var_var_34):
                _var_var_46 = _var_var_40.get(_var_var_37, 1.0)
                _var_var_47 = round(_var_var_46 / _var_var_41 * _var_var_39, 3)
                _var_var_45 += _var_var_47
                _var_var_48 = {"current_yield_kwh": round(_var_var_45, 3)}
                _var_var_49 = await self.morning_routine_handler.update_hourly_actual(
                    date=_var_var_25,
                    hour=_var_var_37,
                    actual_kwh=_var_var_47,
                    sensor_data=_var_var_48,
                )
                if _var_var_49:
                    _var_var_44 += 1
                    _LOGGER.debug(
                        "  Recovered hour %02d: %.3f kWh", _var_var_37, _var_var_47
                    )
            await self._save_yield_cache(
                {
                    "value": _var_var_26,
                    "time": _var_var_24.isoformat(),
                    "date": _var_var_25,
                }
            )
            if _var_var_44 > 0:
                self.coordinator.async_update_listeners()
            _LOGGER.info(
                "Recovered %d/%d missing hourly actuals", _var_var_44, len(_var_var_34)
            )
            return _var_var_44
        except Exception as _var_var_2:
            _LOGGER.error(
                "Error recovering missing hourly actuals: %s", _var_var_2, exc_info=True
            )
            return 0

    async def _execute_morning_routine_unlocked(self) -> bool:
        """Execute the morning routine with fresh data. @zara"""
        try:
            _var_var_25 = dt_util.now().date().isoformat()
            _var_var_24 = dt_util.now()
            _var_var_77 = await self._generate_full_forecast(_var_var_25)
            if not _var_var_77:
                _LOGGER.error("Failed to generate forecast for morning routine")
                return False
            _var_var_78 = _var_var_77.get("hourly", [])
            if not _var_var_78:
                _LOGGER.error("No hourly forecast data in result")
                return False
            _var_var_79 = await self._get_weather_hourly(_var_var_25)
            _var_var_80 = await self._get_astronomy_data(_var_var_25)
            if not _var_var_80:
                _LOGGER.warning(
                    "SFML astronomy provider unavailable or incomplete; morning routine skipped"
                )
                self._ensure_astronomy_recovery("morning_routine")
                return False
            _var_var_81 = self._get_sensor_config()
            _var_var_49 = (
                await self.morning_routine_handler.execute_morning_routine_with_retry(
                    date=_var_var_25,
                    hourly_forecast=_var_var_78,
                    weather_hourly=_var_var_79,
                    astronomy_data=_var_var_80,
                    sensor_config=_var_var_81,
                )
            )
            if _var_var_49:
                _var_var_82 = _var_var_77.get("best_hour")
                _var_var_83 = _var_var_77.get("best_hour_kwh")
                if _var_var_82 is not None:
                    try:
                        await self.coordinator.data_manager.save_forecast_best_hour(
                            hour=_var_var_82,
                            prediction_kwh=_var_var_83 or 0.0,
                            source=_var_var_77.get("method", "morning_routine"),
                        )
                        _LOGGER.info(
                            "Best hour saved: %02d:00 (%.3f kWh)",
                            _var_var_82,
                            _var_var_83 or 0.0,
                        )
                    except Exception as _var_var_2:
                        _LOGGER.warning("Failed to save best hour: %s", _var_var_2)
                _var_var_84 = _var_var_77.get("tomorrow")
                if _var_var_84 is not None:
                    _var_var_85 = _var_var_24 + timedelta(days=1)
                    try:
                        await self.coordinator.data_manager.save_forecast_tomorrow(
                            target_date=_var_var_85,
                            prediction_kwh=_var_var_84,
                            source=_var_var_77.get("method", "morning_routine"),
                            lock=False,
                        )
                        _LOGGER.debug("Tomorrow forecast saved: %.2f kWh", _var_var_84)
                    except Exception as _var_var_2:
                        _LOGGER.warning(
                            "Failed to save tomorrow forecast: %s", _var_var_2
                        )
                _var_var_86 = _var_var_77.get("day_after_tomorrow")
                if _var_var_86 is not None:
                    _var_var_87 = _var_var_24 + timedelta(days=2)
                    try:
                        await self.coordinator.data_manager.save_forecast_day_after(
                            target_date=_var_var_87,
                            prediction_kwh=_var_var_86,
                            source=_var_var_77.get("method", "morning_routine"),
                            lock=False,
                        )
                        _LOGGER.debug("Day-after forecast saved: %.2f kWh", _var_var_86)
                    except Exception as _var_var_2:
                        _LOGGER.warning(
                            "Failed to save day-after forecast: %s", _var_var_2
                        )
                self.coordinator.async_update_listeners()
            return _var_var_49
        except Exception as _var_var_2:
            _LOGGER.error(
                "Morning routine execution failed: %s", _var_var_2, exc_info=True
            )
            return False

    async def _execute_hourly_update(self) -> None:
        """Execute hourly update. @zara"""
        try:
            _LOGGER.debug("Executing hourly update")
            _var_var_24 = dt_util.now()
            _var_var_109 = _var_var_24.astimezone(UTC)
            _var_var_110 = _var_var_109
            _var_var_111 = _var_var_109 - timedelta(hours=1)
            _var_var_112 = _var_var_111.astimezone(_var_var_24.tzinfo)
            _var_var_113 = _var_var_112.date().isoformat()
            _var_var_114 = _var_var_112.hour
            _var_var_48 = await self._collect_sensor_data()
            _var_var_115 = _var_var_48.get("current_yield_kwh")
            if _var_var_115 is None:
                _LOGGER.info(
                    "Hourly consumption update skipped: daily heat-pump energy sensor is unavailable"
                )
                return
            _var_var_116 = await self._load_yield_cache()
            _var_var_117 = str(_var_var_116.get("date") or "")
            _var_var_118 = _var_var_24.date().isoformat()
            if _var_var_113 != _var_var_118:
                _var_var_119 = await self._save_yield_cache(
                    {
                        "value": _var_var_115,
                        "time": _var_var_24.isoformat(),
                        "date": _var_var_118,
                    }
                )
                if not _var_var_119:
                    _LOGGER.info(
                        "New-day EAI baseline is not ready yet; next hourly run will rebaseline safely"
                    )
                _LOGGER.debug(
                    "Hourly consumption update skipped across daily-meter reset for %s hour %02d",
                    _var_var_113,
                    _var_var_114,
                )
                return
            _var_var_120 = dt_util.parse_datetime(str(_var_var_116.get("time") or ""))
            _var_var_121 = False
            _var_var_122 = _var_var_111
            if _var_var_120 is not None:
                _var_var_122 = _var_var_120.astimezone(UTC)
                _var_var_123 = (_var_var_109 - _var_var_122).total_seconds()
                _var_var_121 = 2700 <= _var_var_123 <= 5400
            if _var_var_117 != _var_var_118:
                await self._save_yield_cache(
                    {
                        "value": _var_var_115,
                        "time": _var_var_24.isoformat(),
                        "date": _var_var_118,
                    }
                )
                _LOGGER.info(
                    "Hourly consumption update rebaselined after restart or missing daily cache"
                )
                return
            if _var_var_117 == _var_var_118:
                if not _var_var_121:
                    await self._save_yield_cache(
                        {
                            "value": _var_var_115,
                            "time": _var_var_24.isoformat(),
                            "date": _var_var_118,
                        }
                    )
                    _LOGGER.info(
                        "Hourly consumption update rebaselined after a sensor or scheduler gap"
                    )
                    return
            _var_var_124 = (
                float(_var_var_116.get("value") or 0.0)
                if _var_var_117 == _var_var_118
                else 0.0
            )
            _var_var_125 = float(_var_var_115) - _var_var_124
            if _var_var_125 < 0:
                await self._save_yield_cache(
                    {
                        "value": _var_var_115,
                        "time": _var_var_24.isoformat(),
                        "date": _var_var_118,
                    }
                )
                _LOGGER.info(
                    "Hourly consumption update rebaselined after an unexpected daily-meter reset"
                )
                return
            _var_var_47 = _var_var_125
            _var_var_126 = {
                "temperature_c": _var_var_48.get("temperature_c"),
                "humidity_percent": _var_var_48.get("humidity_percent"),
                "wind_speed_ms": _var_var_48.get("wind_speed_ms"),
                "precipitation_mm": _var_var_48.get("rain_mm"),
                "pressure_hpa": _var_var_48.get("pressure_hpa"),
                "solar_radiation_wm2": _var_var_48.get("solar_radiation_wm2"),
                "lux": _var_var_48.get("lux"),
            }
            _var_var_127 = getattr(
                getattr(self.coordinator, "weather_pipeline_manager", None),
                "weather_actual_tracker",
                None,
            )
            if _var_var_127:
                _var_var_128 = await _var_var_127.get_actual_weather(
                    _var_var_113, _var_var_114
                )
                if _var_var_128:
                    _var_var_126["cloud_cover_percent"] = _var_var_128.get(
                        "cloud_cover_percent"
                    )
                    _var_var_126["frost_detected"] = _var_var_128.get("frost_detected")
                    _var_var_126["frost_score"] = _var_var_128.get("frost_score")
                    _var_var_126["frost_confidence"] = _var_var_128.get(
                        "frost_confidence"
                    )
                    _var_var_126["snow_covered_panels"] = _var_var_128.get(
                        "snow_covered_panels"
                    )
                    if not _var_var_126.get("lux"):
                        if _var_var_128.get("lux"):
                            _var_var_126["lux"] = _var_var_128.get("lux")
            _var_var_129 = None
            _var_var_130 = getattr(self.coordinator, "panel_group_sensor_reader", None)
            if _var_var_130:
                try:
                    _var_var_129 = await _var_var_130.get_all_hourly_productions()
                    if _var_var_129:
                        _LOGGER.debug(
                            "Panel group actuals for hour %d: %s",
                            _var_var_114,
                            _var_var_129,
                        )
                except Exception as _var_var_2:
                    _LOGGER.warning(
                        "Failed to read panel group actuals: %s", _var_var_2
                    )
            _var_var_81 = self._get_sensor_config()
            await self.morning_routine_handler.update_hourly_actual(
                date=_var_var_113,
                hour=_var_var_114,
                actual_kwh=_var_var_47,
                sensor_data=_var_var_48,
                weather_actual=_var_var_126,
                panel_group_actuals=_var_var_129,
                inverter_max_power=getattr(self.coordinator, "inverter_max_power", 0.0)
                or 0.0,
                zero_export_mode=_var_var_81.get("zero_export_mode", False),
                has_battery=_var_var_81.get("has_battery", False),
                solar_to_battery_power=_var_var_48.get("solar_to_battery_power"),
            )
            _var_var_50 = await self.morning_routine_handler._get_astronomy_for_hour(
                _var_var_113, _var_var_114
            )
            _var_var_8, _var_var_9, _var_var_10 = self._measurement_source_contract()
            _var_var_131 = await self._learning_store.async_store_meter_sample(
                date=_var_var_113,
                hour=_var_var_114,
                actual_kwh=_var_var_47,
                sensor_data=_var_var_48,
                weather_actual=_var_var_126,
                astronomy=_var_var_50,
                baseline_kwh=float(_var_var_115),
                baseline_time_local=_var_var_24.isoformat(),
                baseline_date_local=_var_var_118,
                source_entity_id=_var_var_8,
                source_unit=_var_var_9,
                boundary_fingerprint=_var_var_10,
                interval_start_utc=_var_var_122.isoformat().replace("+00:00", "Z"),
                interval_end_utc=_var_var_110.isoformat().replace("+00:00", "Z"),
            )
            if not _var_var_131:
                _LOGGER.warning(
                    "EAI interval was not persisted; baseline intentionally not advanced"
                )
                return
            self.coordinator.async_update_listeners()
            if self._kalman_tracker and 7 <= _var_var_114 <= 19:
                _var_var_132 = _var_var_48.get("solar_radiation_wm2")
                if _var_var_132 is not None:
                    try:
                        _var_var_133 = await self._get_forecast_radiation_for_hour(
                            _var_var_113, _var_var_114
                        )
                        if _var_var_133 is not None and _var_var_133 > 0:
                            _var_var_134 = _var_var_132 - _var_var_133
                            self._kalman_tracker.update(_var_var_134)
                    except Exception as _var_var_2:
                        _LOGGER.debug("Kalman update skipped: %s", _var_var_2)
        except Exception as _var_var_2:
            _LOGGER.error("Hourly update failed: %s", _var_var_2, exc_info=True)
        if True:
            return None
        else:
            return None

    async def _train_weather_mlp(self, current_time) -> None:
        """Train Weather MLP on historical precision data. @zara"""
        if self._weather_mlp is None:
            return
        import math as _var_var_135

        _var_var_28 = await self.db.fetchall(
            "SELECT wpd.hour, wpd.solar_forecast, wpd.solar_actual, wpd.solar_factor,\n                      wco.cloud_cover, wco.humidity, wco.pressure, wco.wind_speed,\n                      wpd.date\n               FROM weather_precision_daily wpd\n               JOIN weather_cache_open_meteo wco\n                    ON wpd.date = wco.forecast_date AND wpd.hour = wco.hour\n               WHERE wpd.solar_forecast > 10\n                 AND wpd.solar_actual IS NOT NULL\n                 AND wpd.date >= date(?, '-30 days')\n               ORDER BY wpd.date, wpd.hour",
            (current_time.date().isoformat(),),
        )
        if not _var_var_28 or len(_var_var_28) < 50:
            _LOGGER.info(
                "Weather MLP: insufficient training data (%d samples, need 50)",
                len(_var_var_28) if _var_var_28 else 0,
            )
            return
        _var_var_136 = []
        _var_var_137 = []
        for _var_var_36 in _var_var_28:
            try:
                _var_var_138 = datetime.fromisoformat(str(_var_var_36[8])).date()
                _var_var_37 = int(_var_var_36[0])
                _var_var_139 = float(_var_var_36[1])
                _var_var_140 = float(_var_var_36[3])
                _var_var_141 = float(
                    _var_var_36[4] if _var_var_36[4] is not None else 50.0
                )
                _var_var_142 = float(
                    _var_var_36[5] if _var_var_36[5] is not None else 70.0
                )
                _var_var_143 = float(
                    _var_var_36[6] if _var_var_36[6] is not None else 1013.0
                )
                _var_var_144 = float(
                    _var_var_36[7] if _var_var_36[7] is not None else 3.0
                )
            except (TypeError, ValueError, OverflowError):
                continue
            _var_var_145 = (
                _var_var_139,
                _var_var_140,
                _var_var_141,
                _var_var_142,
                _var_var_143,
                _var_var_144,
            )
            if (
                _var_var_37 not in range(24)
                or not all(
                    (
                        (_var_var_135.isfinite if True else None)(_var_var_11)
                        for _var_var_11 in _var_var_145
                    )
                )
                or _var_var_139 <= 10
                or (not 0 < _var_var_140 <= 3.0)
            ):
                continue
            _var_var_136.append(
                (
                    _var_var_37,
                    _var_var_139,
                    _var_var_36[2],
                    _var_var_140,
                    _var_var_141,
                    _var_var_142,
                    _var_var_143,
                    _var_var_144,
                    _var_var_138.isoformat(),
                )
            )
            _var_var_137.append(_var_var_138)
            continue
        if len(_var_var_136) < 50:
            _LOGGER.info(
                "Weather MLP: insufficient date-valid training data (%d samples, need 50)",
                len(_var_var_136),
            )
            return
        _var_var_146 = min(_var_var_137)
        _var_var_147 = max(_var_var_137)
        _var_var_50 = await self.astronomy_provider.async_get_legacy_days(
            _var_var_146, days=(_var_var_147 - _var_var_146).days + 1
        )
        if not _var_var_50:
            _LOGGER.info("Weather MLP: SFML astronomy unavailable; training skipped")
            return
        _var_var_148 = []
        _var_var_149 = []
        for _var_var_36 in _var_var_136:
            (
                _var_var_37,
                _var_var_139,
                _var_var_150,
                _var_var_140,
                _var_var_141,
                _var_var_142,
                _var_var_143,
                _var_var_144,
                _var_var_100,
            ) = _var_var_36
            _var_var_151 = _var_var_50.get(f"{_var_var_100}_{_var_var_37:02d}", {}).get(
                "sun_elevation_deg"
            )
            if _var_var_151 is None or _var_var_151 <= 5:
                continue
            _var_var_152 = [
                _var_var_139 / 1200.0,
                (_var_var_141 or 50.0) / 100.0,
                max(0.0, min(1.0, (_var_var_151 or 0.0) / 90.0)),
                (_var_var_142 or 70.0) / 100.0,
                ((_var_var_143 or 1013.0) - 950.0) / 100.0,
                (_var_var_144 or 3.0) / 30.0,
                (_var_var_135.sin if True else None)(
                    2.0 * _var_var_135.pi * _var_var_37 / 24.0
                ),
                (_var_var_135.cos if True else None)(
                    2.0 * _var_var_135.pi * _var_var_37 / 24.0
                ),
            ]
            _var_var_153 = min(2.0, max(0.1, _var_var_140))
            _var_var_148.append(_var_var_152)
            _var_var_149.append(_var_var_153)
        if len(_var_var_148) < 50:
            _LOGGER.info(
                "Weather MLP: after filtering only %d valid samples", len(_var_var_148)
            )
            return
        _LOGGER.info("Weather MLP training: %d samples, 8 features", len(_var_var_148))
        _var_var_99 = await self._weather_mlp.train(
            X_data=_var_var_148,
            y_targets=_var_var_149,
            epochs=50,
            validation_split=0.2,
            early_stopping_patience=10,
        )
        if _var_var_99.get("success"):
            _var_var_4 = self._weather_mlp.get_weights()
            await self.db.save_weather_mlp_weights(_var_var_4)
            self._attach_mlp_to_correctors()
            _LOGGER.info(
                "Weather MLP trained and saved: R²=%.3f, RMSE=%.4f, %d samples",
                _var_var_99.get("accuracy", 0.0),
                _var_var_99.get("rmse", 0.0),
                _var_var_99.get("training_samples", 0),
            )
        else:
            _LOGGER.warning("Weather MLP training failed")
        if True:
            return

    async def _collect_sensor_data(self) -> Dict[str, Any]:
        """Collect current sensor data. @zara"""
        _var_var_48 = {}
        if self.wp_energy_today_entity_id:
            _var_var_15 = self.hass.states.get(self.wp_energy_today_entity_id)
            if _var_var_15 and _var_var_15.state not in (
                None,
                "unknown",
                "unavailable",
            ):
                _var_var_154 = self._energy_state_to_kwh(_var_var_15)
                if _var_var_154 is not None:
                    _var_var_48["current_yield_kwh"] = _var_var_154
        _var_var_17 = self.coordinator.config_entry
        _var_var_18 = {**_var_var_17.data, **_var_var_17.options}
        _var_var_155 = _var_var_18.get(
            CONF_ELECTRICAL_MEASUREMENT_TOPOLOGY
        ) == "separate" and is_true(_var_var_18, CONF_HAS_HEATING_ELEMENT)
        if _var_var_155:
            _var_var_21 = _var_var_18.get(CONF_HEATING_ELEMENT_ENERGY_TODAY_ENTITY)
            _var_var_22 = (
                self.hass.states.get(_var_var_21)
                if isinstance(_var_var_21, str) and _var_var_21
                else None
            )
            _var_var_156 = (
                self._energy_state_to_kwh(_var_var_22)
                if _var_var_22
                and _var_var_22.state not in (None, "unknown", "unavailable")
                else None
            )
            _var_var_157 = _var_var_48.get("current_yield_kwh")
            if _var_var_157 is None or _var_var_156 is None:
                _var_var_48.pop("current_yield_kwh", None)
            else:
                _var_var_48["current_yield_kwh"] = _var_var_157 + _var_var_156
        _var_var_81 = self._get_sensor_config()
        _var_var_158 = {
            "temp_sensor": "temperature_c",
            "humidity_sensor": "humidity_percent",
            "pressure_sensor": "pressure_hpa",
            "wind_sensor": "wind_speed_ms",
            "rain_sensor": "rain_mm",
            "solar_radiation_sensor": "solar_radiation_wm2",
            "lux_sensor": "lux",
        }
        for _var_var_159, _var_var_160 in _var_var_81.items():
            if _var_var_159 not in _var_var_158:
                continue
            if _var_var_160:
                if isinstance(_var_var_160, str):
                    _var_var_15 = self.hass.states.get(_var_var_160)
                    if _var_var_15:
                        if _var_var_15.state not in (None, "unknown", "unavailable"):
                            try:
                                _var_var_161 = _var_var_158[_var_var_159]
                                _var_var_48[_var_var_161] = float(_var_var_15.state)
                            except (ValueError, TypeError):
                                continue
        _var_var_162 = _var_var_81.get("solar_to_battery_sensor")
        if _var_var_162 and isinstance(_var_var_162, str):
            _var_var_15 = self.hass.states.get(_var_var_162)
            if _var_var_15 and _var_var_15.state not in (
                None,
                "unknown",
                "unavailable",
            ):
                try:
                    _var_var_48["solar_to_battery_power"] = float(_var_var_15.state)
                except (ValueError, TypeError):
                    pass
        if True:
            return _var_var_48

    async def _execute_midnight_task(self) -> None:
        """Execute midnight task. @zara"""
        try:
            _LOGGER.info("Executing midnight task")
            if hasattr(self.coordinator, "_peak_power_today"):
                self.coordinator._peak_power_today = 0.0
                self.coordinator._peak_time_today = None
                try:
                    _var_var_25 = dt_util.now().date().isoformat()
                    await self.coordinator.data_manager._db_manager.execute(
                        "UPDATE production_time_state\n                           SET peak_power_w = 0, peak_power_time = NULL,\n                               date = ?, last_updated = ?\n                           WHERE id = 1",
                        (_var_var_25, dt_util.now().isoformat()),
                    )
                except Exception:
                    pass
                _LOGGER.debug("Reset daily peak tracking")
            await self.coordinator.reset_expected_daily_consumption()
            await self._schedule_dynamic_tasks_for_today()
            if self._kalman_tracker:
                self._kalman_tracker.reset()
        except Exception as _var_var_2:
            _LOGGER.error("Midnight task failed: %s", _var_var_2, exc_info=True)
        if True:
            return None

    async def _finalize_day_internal(self, now: datetime) -> None:
        """Internal: Finalize current day with actual values. @zara"""
        try:
            _var_var_171 = 0.0
            if self.wp_energy_today_entity_id:
                _var_var_27 = self.hass.states.get(self.wp_energy_today_entity_id)
                if _var_var_27 and _var_var_27.state not in (
                    None,
                    "unknown",
                    "unavailable",
                ):
                    try:
                        _var_var_171 = float(_var_var_27.state)
                    except (ValueError, TypeError):
                        pass
            _var_var_172 = None
            _var_var_173 = getattr(self.coordinator, "total_consumption_today", None)
            if _var_var_173:
                _var_var_174 = self.hass.states.get(_var_var_173)
                if _var_var_174 and _var_var_174.state not in (
                    None,
                    "unknown",
                    "unavailable",
                ):
                    try:
                        _var_var_172 = float(_var_var_174.state)
                    except (ValueError, TypeError):
                        pass
            _var_var_175 = 0
            try:
                if (
                    hasattr(self.coordinator, "production_time_calculator")
                    and self.coordinator.production_time_calculator
                ):
                    _var_var_176 = (
                        self.coordinator.production_time_calculator.get_production_hours()
                    )
                    if _var_var_176 is not None:
                        _var_var_175 = int(_var_var_176 * 3600)
            except Exception:
                pass
            _var_var_177 = None
            try:
                _var_var_169 = now.strftime("%Y-%m-%d")
                _var_var_28 = await self.db.fetchall(
                    "SELECT exclude_from_learning, has_weather_alert, inverter_clipped,\n                              weather_alert_type, mppt_throttled\n                       FROM hourly_predictions\n                       WHERE target_date = ?",
                    (_var_var_169,),
                )
                if _var_var_28:
                    _var_var_178 = 0
                    _var_var_179 = len(_var_var_28)
                    _var_var_180 = {}
                    for _var_var_36 in _var_var_28:
                        _var_var_181 = _var_var_36[0]
                        _var_var_182 = _var_var_36[1]
                        _var_var_183 = _var_var_36[2]
                        _var_var_184 = _var_var_36[3]
                        _var_var_185 = _var_var_36[4]
                        if _var_var_181 or _var_var_182 or _var_var_183 or _var_var_185:
                            _var_var_178 += 1
                            _var_var_186 = _var_var_184 or "other"
                            if _var_var_183:
                                _var_var_186 = "inverter_clipped"
                            elif _var_var_185:
                                _var_var_186 = "mppt_throttled"
                            _var_var_180[_var_var_186] = (
                                _var_var_180.get(_var_var_186, 0) + 1
                            )
                    _var_var_187 = (
                        _var_var_178 / _var_var_179 if _var_var_179 > 0 else 0.0
                    )
                    _var_var_177 = {
                        "count": _var_var_178,
                        "total": _var_var_179,
                        "ratio": round(_var_var_187, 3),
                        "reasons": _var_var_180 if _var_var_180 else None,
                    }
                    if _var_var_178 > 0:
                        _LOGGER.info(
                            f"Excluded hours info for {_var_var_169}: {_var_var_178}/{_var_var_179} ({_var_var_187:.1%}), reasons: {_var_var_180}"
                        )
            except Exception as _var_var_2:
                _LOGGER.debug(f"Could not calculate excluded hours info: {_var_var_2}")
            _var_var_188 = None
            if _var_var_171 > 0:
                _var_var_189 = await self.db.fetchone(
                    "SELECT prediction_kwh FROM daily_forecasts\n                       WHERE forecast_type = 'today' AND forecast_date = ?",
                    (now.date().isoformat(),),
                )
                if _var_var_189 and _var_var_189[0]:
                    _var_var_190 = _var_var_189[0]
                    _var_var_191 = abs(_var_var_190 - _var_var_171)
                    if _var_var_171 > 0.1:
                        _var_var_188 = max(
                            0.0, (1.0 - _var_var_191 / _var_var_171) * 100.0
                        )
            _var_var_192 = {
                "date": now.date().isoformat(),
                "finalized": {
                    "yield_kwh": _var_var_171,
                    "consumption_kwh": _var_var_172,
                    "production_hours": (
                        f"{_var_var_175 // 3600}:{_var_var_175 % 3600 // 60:02d}"
                        if _var_var_175 > 0
                        else None
                    ),
                    "accuracy_percent": (
                        round(_var_var_188, 1) if _var_var_188 is not None else None
                    ),
                    "excluded_hours": _var_var_177,
                    "at": now.isoformat(),
                },
            }
            await self.db.save_daily_forecast_tracking(_var_var_192)
            _LOGGER.info(
                f"Day finalized: yield={_var_var_171:.2f} kWh, consumption={(_var_var_172 if _var_var_172 is not None else 0.0):.2f} kWh, accuracy={(_var_var_188 if _var_var_188 is not None else 0.0):.1f}%"
            )
        except Exception as _var_var_2:
            _LOGGER.error(f"Failed to finalize day: {_var_var_2}", exc_info=True)
        return None if True else None

    async def _calculate_stats_internal(self, now: datetime) -> None:
        """Internal: Calculate statistics (all-time peak, current week/month, last N days). @zara"""
        try:
            _var_var_206 = {}
            _var_var_207 = await self.db.fetchone(
                "SELECT MAX(peak_power), date FROM forecast_history\n                   WHERE peak_power IS NOT NULL"
            )
            if _var_var_207 and _var_var_207[0]:
                _var_var_206["all_time_peak"] = {
                    "power_w": _var_var_207[0],
                    "date": str(_var_var_207[1]) if _var_var_207[1] else None,
                    "at": None,
                }
            _var_var_25 = now.date()
            _var_var_208 = _var_var_25 - timedelta(days=_var_var_25.weekday())
            _var_var_209 = await self.db.fetchall(
                "SELECT actual_kwh, consumption_kwh FROM forecast_history\n                   WHERE date >= ? AND date <= ?",
                (_var_var_208.isoformat(), _var_var_25.isoformat()),
            )
            if _var_var_209:
                _var_var_210 = sum(
                    (_var_var_36[0] or 0 for _var_var_36 in _var_var_209)
                )
                _var_var_211 = sum(
                    (
                        _var_var_36[1] or 0
                        for _var_var_36 in _var_var_209
                        if _var_var_36[1]
                    )
                )
                _var_var_206["current_week"] = {
                    "period": f"KW {_var_var_25.isocalendar()[1]}",
                    "date_range": f"{_var_var_208.strftime('%d.%m')} - {_var_var_25.strftime('%d.%m')}",
                    "yield_kwh": round(_var_var_210, 2),
                    "consumption_kwh": (
                        round(_var_var_211, 2) if _var_var_211 > 0 else None
                    ),
                    "days": len(_var_var_209),
                    "updated_at": now.isoformat(),
                }
            _var_var_212 = _var_var_25.replace(day=1)
            _var_var_213 = await self.db.fetchall(
                "SELECT actual_kwh, consumption_kwh, autarky FROM forecast_history\n                   WHERE date >= ? AND date <= ?",
                (_var_var_212.isoformat(), _var_var_25.isoformat()),
            )
            if _var_var_213:
                _var_var_214 = sum(
                    (_var_var_36[0] or 0 for _var_var_36 in _var_var_213)
                )
                _var_var_215 = sum(
                    (
                        _var_var_36[1] or 0
                        for _var_var_36 in _var_var_213
                        if _var_var_36[1]
                    )
                )
                _var_var_216 = [
                    _var_var_36[2]
                    for _var_var_36 in _var_var_213
                    if _var_var_36[2] is not None
                ]
                _var_var_217 = (
                    sum(_var_var_216) / len(_var_var_216) if _var_var_216 else None
                )
                _var_var_206["current_month"] = {
                    "period": _var_var_25.strftime("%B %Y"),
                    "yield_kwh": round(_var_var_214, 2),
                    "consumption_kwh": (
                        round(_var_var_215, 2) if _var_var_215 > 0 else None
                    ),
                    "avg_autarky": round(_var_var_217, 1) if _var_var_217 else None,
                    "days": len(_var_var_213),
                    "updated_at": now.isoformat(),
                }
            _var_var_218 = (_var_var_25 - timedelta(days=7)).isoformat()
            _var_var_219 = await self.db.fetchall(
                "SELECT actual_kwh, accuracy FROM forecast_history\n                   WHERE date >= ? AND date < ?",
                (_var_var_218, _var_var_25.isoformat()),
            )
            if _var_var_219:
                _var_var_220 = sum(
                    (_var_var_36[0] or 0 for _var_var_36 in _var_var_219)
                )
                _recovered_accuracies = [
                    _var_var_36[1]
                    for _var_var_36 in _var_var_219
                    if _var_var_36[1] is not None
                ]
                _recovered_average = (
                    sum(_recovered_accuracies) / len(_recovered_accuracies)
                    if _recovered_accuracies
                    else None
                )
                _var_var_206["last_7_days"] = {
                    "avg_yield_kwh": round(_var_var_220 / len(_var_var_219), 2),
                    "avg_accuracy": (
                        round(_recovered_average, 1) if _recovered_average else None
                    ),
                    "total_yield_kwh": round(_var_var_220, 2),
                    "calculated_at": now.isoformat(),
                }
            _var_var_223 = (_var_var_25 - timedelta(days=30)).isoformat()
            _var_var_224 = await self.db.fetchall(
                "SELECT actual_kwh, accuracy FROM forecast_history\n                   WHERE date >= ? AND date < ?",
                (_var_var_223, _var_var_25.isoformat()),
            )
            if _var_var_224:
                _var_var_220 = sum(
                    (_var_var_36[0] or 0 for _var_var_36 in _var_var_224)
                )
                _recovered_accuracies = [
                    _var_var_36[1]
                    for _var_var_36 in _var_var_224
                    if _var_var_36[1] is not None
                ]
                _recovered_average = (
                    sum(_recovered_accuracies) / len(_recovered_accuracies)
                    if _recovered_accuracies
                    else None
                )
                _var_var_206["last_30_days"] = {
                    "avg_yield_kwh": round(_var_var_220 / len(_var_var_224), 2),
                    "avg_accuracy": (
                        round(_recovered_average, 1) if _recovered_average else None
                    ),
                    "total_yield_kwh": round(_var_var_220, 2),
                    "calculated_at": now.isoformat(),
                }
            _var_var_225 = (_var_var_25 - timedelta(days=365)).isoformat()
            _var_var_226 = await self.db.fetchall(
                "SELECT actual_kwh FROM forecast_history\n                   WHERE date >= ? AND date < ?",
                (_var_var_225, _var_var_25.isoformat()),
            )
            if _var_var_226:
                _var_var_220 = sum(
                    (_var_var_36[0] or 0 for _var_var_36 in _var_var_226)
                )
                _var_var_206["last_365_days"] = {
                    "avg_yield_kwh": round(_var_var_220 / len(_var_var_226), 2),
                    "total_yield_kwh": round(_var_var_220, 2),
                    "calculated_at": now.isoformat(),
                }
            await self.db.save_daily_statistics(_var_var_206)
            _LOGGER.info("Statistics calculated successfully")
        except Exception as _var_var_2:
            _LOGGER.error(
                f"Failed to calculate statistics: {_var_var_2}", exc_info=True
            )
        if True:
            return None

    async def _train_ml_main_model(self, now: datetime) -> None:
        """Train the AI model. @zara"""
        try:
            if not self.coordinator or not self.coordinator.ai_predictor:
                _LOGGER.debug("AI model training skipped: no predictor available")
                return
            _var_var_130 = getattr(self.coordinator, "panel_group_sensor_reader", None)
            if _var_var_130:
                try:
                    _var_var_227 = (
                        await _var_var_130.backfill_missing_actuals_from_recorder(
                            days=30
                        )
                    )
                    if _var_var_227 > 0:
                        _LOGGER.info(
                            "Backfilled %d per-group actuals from recorder before training",
                            _var_var_227,
                        )
                except Exception as _var_var_2:
                    _LOGGER.warning("Recorder backfill failed: %s", _var_var_2)
            _var_var_228 = self.coordinator.ai_predictor
            _var_var_99 = await _var_var_228.train_model()
            if _var_var_99.success:
                _var_var_229 = ", attention=ON" if _var_var_99.has_attention else ""
                _var_var_230 = (
                    f", RMSE={_var_var_99.rmse:.3f}kWh" if _var_var_99.rmse else ""
                )
                _LOGGER.info(
                    f"AI model trained: R²={_var_var_99.accuracy:.3f}{_var_var_230}, samples={_var_var_99.samples_used}, features={_var_var_99.feature_count}, outputs={_var_var_99.num_outputs}{_var_var_229}"
                )
                if self.coordinator:
                    self.coordinator.on_ai_training_complete(
                        timestamp=dt_util.now(), accuracy=_var_var_99.accuracy
                    )
            elif _var_var_99.reason_code == REASON_RIDGE_WARMUP:
                _LOGGER.info(
                    "AI model warm-up in progress: %d/%d valid Ridge samples",
                    _var_var_99.samples_used,
                    MIN_RIDGE_SAMPLES,
                )
            elif _var_var_99.error_message:
                _LOGGER.warning(
                    f"AI model training incomplete: {_var_var_99.error_message}"
                )
            else:
                _LOGGER.debug("AI model training needs more data")
            if _var_var_228.dni_tracker:
                try:
                    await _var_var_228.dni_tracker.end_of_day_update()
                    _var_var_228.dni_tracker.start_new_day()
                except Exception as _var_var_2:
                    _LOGGER.warning(f"DNI tracker finalize error: {_var_var_2}")
        except Exception as _var_var_2:
            _LOGGER.error(f"AI model training error: {_var_var_2}", exc_info=True)
        return None if True else None

    async def migrate_daily_consumption_model(self) -> dict:
        """One-shot relearn from stored hours, then rewrite unlocked future hours.

        Too little history changes nothing and leaves the marker unset so a
        later start can try again. A set marker returns without another pass.
        """
        try:
            _var_var_258 = getattr(self.coordinator, "thermodynamics_calibrator", None)
            if _var_var_258 is None or not hasattr(
                _var_var_258, "run_history_migration"
            ):
                return {"ran": False, "reason": "no_calibrator", "elapsed_s": 0.0}
            if await _var_var_258.migration_already_done():
                return {"ran": False, "reason": "already", "elapsed_s": 0.0}
            _var_var_28 = await self.db.fetchall(
                "SELECT hp.target_date, hp.target_hour, hp.actual_kwh, hp.physics_kwh,\n                          COALESCE(pwa.temperature, pwf.temperature),\n                          COALESCE(pwa.wind, pwf.wind, 0),\n                          hp.exclude_from_learning\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_weather pwa\n                       ON hp.prediction_id = pwa.prediction_id\n                       AND pwa.weather_type = 'actual'\n                   LEFT JOIN prediction_weather pwf\n                       ON hp.prediction_id = pwf.prediction_id\n                       AND pwf.weather_type = 'forecast'\n                   WHERE hp.actual_kwh IS NOT NULL\n                     AND hp.physics_kwh IS NOT NULL"
            )
            _var_var_259 = getattr(self.coordinator, "forecast_orchestrator", None)
            _var_var_260 = getattr(_var_var_259, "rule_based_strategy", None)
            try:
                _var_var_292 = float(
                    getattr(_var_var_260, "heating_capacity_kw", 0.0) or 0.0
                )
            except (TypeError, ValueError):
                _var_var_292 = 0.0
            _var_var_99 = await _var_var_258.run_history_migration(
                list(_var_var_28 or []), heating_capacity_kw=_var_var_292
            )
            if _var_var_99.get("ran"):
                _var_var_99["rewritten_hours"] = (
                    await self.rewrite_unlocked_future_hours()
                )
            return _var_var_99
        except Exception as _var_var_191:
            _LOGGER.warning("Daily consumption migration failed: %s", _var_var_191)
            return {"ran": False, "reason": "error", "elapsed_s": 0.0}

    async def _train_panel_group_efficiency(self, now: datetime) -> None:
        """Learn per-group ensemble weights (LSTM vs Ridge) per condition bucket. @zara V16.0.3

        For each panel group, computes LSTM and Ridge MAE against actual production,
        bucketed by cloud cover and hour of day. Derives inverse-MAE weights with
        EMA smoothing. Silent no-op when lstm_kwh/ridge_kwh data is not yet available.
        """
        try:
            _var_var_231 = self.db
            _var_var_25 = now.date().isoformat()
            _var_var_28 = await _var_var_231.fetchall(
                "SELECT ppg.group_name, hp.target_hour,\n                          ppg.lstm_kwh, ppg.ridge_kwh, ppg.actual_kwh,\n                          pw.clouds\n                   FROM prediction_panel_groups ppg\n                   JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   WHERE hp.target_date = ?\n                     AND ppg.actual_kwh IS NOT NULL AND ppg.actual_kwh > 0\n                     AND ppg.lstm_kwh IS NOT NULL AND ppg.ridge_kwh IS NOT NULL\n                     AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                     AND (ppg.exclude_from_learning_group = FALSE OR ppg.exclude_from_learning_group IS NULL)",
                (_var_var_25,),
            )
            if not _var_var_28:
                _LOGGER.debug(
                    "Ensemble group weight learning: No data with lstm/ridge/actual for %s",
                    _var_var_25,
                )
                return
            _var_var_237 = {}
            for _var_var_36 in _var_var_28:
                _var_var_306 = _var_var_36[0]
                _var_var_37 = _var_var_36[1]
                _var_var_307 = _var_var_36[2]
                _var_var_308 = _var_var_36[3]
                _var_var_38 = _var_var_36[4]
                _var_var_241 = _var_var_36[5] or 50
                _var_var_242 = (
                    "clear"
                    if _var_var_241 < 30
                    else "partly_cloudy" if _var_var_241 < 70 else "overcast"
                )
                _var_var_243 = (
                    "morning"
                    if _var_var_37 < 10
                    else "midday" if _var_var_37 < 15 else "afternoon"
                )
                _var_var_244 = (_var_var_306, _var_var_242, _var_var_243)
                if _var_var_244 not in _var_var_237:
                    _var_var_237[_var_var_244] = {"lstm": [], "ridge": []}
                _var_var_237[_var_var_244]["lstm"].append(
                    abs(_var_var_307 - _var_var_38)
                )
                _var_var_237[_var_var_244]["ridge"].append(
                    abs(_var_var_308 - _var_var_38)
                )
            _var_var_245 = 0.3
            _var_var_309 = 0.2
            _var_var_310 = 0.95
            _var_var_246 = get_season(now.month)
            _var_var_311 = 0
            for (
                _var_var_306,
                _var_var_242,
                _var_var_243,
            ), _var_var_166 in _var_var_237.items():
                _var_var_312 = sum(_var_var_166["lstm"]) / len(_var_var_166["lstm"])
                _var_var_313 = sum(_var_var_166["ridge"]) / len(_var_var_166["ridge"])
                for _var_var_250 in (None, _var_var_246):
                    _var_var_62 = await _var_var_231.fetchone(
                        "SELECT lstm_mae, ridge_mae, sample_count\n                           FROM ensemble_group_weights\n                           WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                               AND season IS ?",
                        (_var_var_306, _var_var_242, _var_var_243, _var_var_250),
                    )
                    if _var_var_62 and _var_var_62[2] and (_var_var_62[2] > 0):
                        _var_var_314 = (
                            _var_var_245 * _var_var_312
                            + (1 - _var_var_245) * _var_var_62[0]
                        )
                        _var_var_315 = (
                            _var_var_245 * _var_var_313
                            + (1 - _var_var_245) * _var_var_62[1]
                        )
                        _var_var_254 = _var_var_62[2] + len(_var_var_166["lstm"])
                    else:
                        _var_var_314 = _var_var_312
                        _var_var_315 = _var_var_313
                        _var_var_254 = len(_var_var_166["lstm"])
                    _var_var_316 = max(0.001, _var_var_314)
                    _var_var_317 = max(0.001, _var_var_315)
                    _var_var_318 = 1.0 / _var_var_316 + 1.0 / _var_var_317
                    _var_var_319 = 1.0 / _var_var_316 / _var_var_318
                    _var_var_320 = max(_var_var_309, min(_var_var_310, _var_var_319))
                    _var_var_321 = 1.0 - _var_var_320
                    _var_var_257 = (
                        _var_var_306,
                        _var_var_242,
                        _var_var_243,
                        _var_var_250,
                        round(_var_var_320, 4),
                        round(_var_var_321, 4),
                        round(_var_var_314, 6),
                        round(_var_var_315, 6),
                        _var_var_254,
                        now,
                    )
                    if _var_var_250 is None:
                        async with _var_var_231.transaction():
                            await _var_var_231.execute(
                                "DELETE FROM ensemble_group_weights\n                                   WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                                       AND season IS NULL",
                                (_var_var_306, _var_var_242, _var_var_243),
                                auto_commit=False,
                            )
                            await _var_var_231.execute(
                                "INSERT INTO ensemble_group_weights\n                                   (group_name, cloud_bucket, hour_bucket, season, lstm_weight, ridge_weight,\n                                    lstm_mae, ridge_mae, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                _var_var_257,
                                auto_commit=False,
                            )
                    else:
                        await _var_var_231.execute(
                            "INSERT INTO ensemble_group_weights\n                               (group_name, cloud_bucket, hour_bucket, season, lstm_weight, ridge_weight,\n                                lstm_mae, ridge_mae, sample_count, last_updated)\n                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                               ON CONFLICT(group_name, cloud_bucket, hour_bucket, season) DO UPDATE SET\n                                   lstm_weight = excluded.lstm_weight,\n                                   ridge_weight = excluded.ridge_weight,\n                                   lstm_mae = excluded.lstm_mae,\n                                   ridge_mae = excluded.ridge_mae,\n                                   sample_count = excluded.sample_count,\n                                   last_updated = excluded.last_updated",
                            _var_var_257,
                            auto_commit=True,
                        )
                    _var_var_311 += 1
                _LOGGER.info(
                    "Ensemble weight [%s/%s/%s]: lstm=%.2f ridge=%.2f (lstm_mae=%.4f, ridge_mae=%.4f, %d samples)",
                    _var_var_306,
                    _var_var_242,
                    _var_var_243,
                    _var_var_320,
                    _var_var_321,
                    _var_var_314,
                    _var_var_315,
                    _var_var_254,
                )
                continue
            _LOGGER.info(
                "Ensemble group weight learning completed for %s: %d buckets updated",
                _var_var_25,
                _var_var_311,
            )
        except Exception as _var_var_2:
            _LOGGER.error(
                f"Ensemble group weight learning error: {_var_var_2}", exc_info=True
            )
        return None if True else None

    async def _learn_group_method_performance(self, now: datetime) -> None:
        """Learn per-group method accuracy (Physics vs AI vs LSTM vs Ridge). @zara V16.4.0

        For each panel group, computes MAE of each prediction method against actual
        production, bucketed by (cloud_bucket, hour_bucket, season). Determines which
        method is most accurate per group per condition. This data feeds the graduated
        safeguard to make data-driven decisions about AI vs Physics blending.

        Silent no-op when no per-group actual data is available.
        """
        try:
            _var_var_231 = self.db
            _var_var_25 = now.date().isoformat()
            _var_var_28 = await _var_var_231.fetchall(
                "SELECT ppg.group_name, hp.target_hour,\n                          ppg.physics_kwh, ppg.ai_kwh,\n                          ppg.lstm_kwh, ppg.ridge_kwh,\n                          ppg.actual_kwh,\n                          pw.clouds\n                   FROM prediction_panel_groups ppg\n                   JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                   LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                       AND pw.weather_type = 'forecast'\n                   WHERE hp.target_date = ?\n                     AND ppg.actual_kwh IS NOT NULL AND ppg.actual_kwh > 0\n                     AND ppg.physics_kwh IS NOT NULL\n                     AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                     AND (ppg.exclude_from_learning_group = FALSE OR ppg.exclude_from_learning_group IS NULL)",
                (_var_var_25,),
            )
            if not _var_var_28:
                _LOGGER.debug(
                    "Group method performance: No per-group actual data for %s",
                    _var_var_25,
                )
                return
            _var_var_237 = {}
            for _var_var_36 in _var_var_28:
                _var_var_306 = _var_var_36[0]
                _var_var_37 = _var_var_36[1]
                _var_var_322 = _var_var_36[2]
                _var_var_323 = _var_var_36[3]
                _var_var_307 = _var_var_36[4]
                _var_var_308 = _var_var_36[5]
                _var_var_38 = _var_var_36[6]
                _var_var_241 = _var_var_36[7] or 50
                _var_var_242 = (
                    "clear"
                    if _var_var_241 < 30
                    else "partly_cloudy" if _var_var_241 < 70 else "overcast"
                )
                _var_var_243 = (
                    "morning"
                    if _var_var_37 < 10
                    else "midday" if _var_var_37 < 15 else "afternoon"
                )
                _var_var_244 = (_var_var_306, _var_var_242, _var_var_243)
                if _var_var_244 not in _var_var_237:
                    _var_var_237[_var_var_244] = {
                        "physics": [],
                        "ai": [],
                        "lstm": [],
                        "ridge": [],
                        "blend": [],
                    }
                _var_var_237[_var_var_244]["physics"].append(
                    abs(_var_var_322 - _var_var_38)
                )
                if _var_var_323 is not None and _var_var_323 > 0:
                    _var_var_237[_var_var_244]["ai"].append(
                        abs(_var_var_323 - _var_var_38)
                    )
                if _var_var_307 is not None and _var_var_307 > 0:
                    _var_var_237[_var_var_244]["lstm"].append(
                        abs(_var_var_307 - _var_var_38)
                    )
                if _var_var_308 is not None and _var_var_308 > 0:
                    _var_var_237[_var_var_244]["ridge"].append(
                        abs(_var_var_308 - _var_var_38)
                    )
            if not _var_var_237:
                return
            _var_var_245 = 0.3
            _var_var_246 = get_season(now.month)
            _var_var_311 = 0
            for (
                _var_var_306,
                _var_var_242,
                _var_var_243,
            ), _var_var_166 in _var_var_237.items():
                _var_var_247 = sum(_var_var_166["physics"]) / len(
                    _var_var_166["physics"]
                )
                _var_var_249 = (
                    sum(_var_var_166["ai"]) / len(_var_var_166["ai"])
                    if _var_var_166["ai"]
                    else None
                )
                _var_var_312 = (
                    sum(_var_var_166["lstm"]) / len(_var_var_166["lstm"])
                    if _var_var_166["lstm"]
                    else None
                )
                _var_var_313 = (
                    sum(_var_var_166["ridge"]) / len(_var_var_166["ridge"])
                    if _var_var_166["ridge"]
                    else None
                )
                _var_var_324 = {"physics": _var_var_247}
                if _var_var_249 is not None:
                    _var_var_324["ai"] = _var_var_249
                if _var_var_312 is not None:
                    _var_var_324["lstm"] = _var_var_312
                if _var_var_313 is not None:
                    _var_var_324["ridge"] = _var_var_313
                _var_var_325 = min(_var_var_324, key=_var_var_324.get)
                if _var_var_249 and _var_var_249 > 0 and (_var_var_247 > 0):
                    _var_var_326 = _var_var_247 / _var_var_249
                    _var_var_256 = max(0.5, min(2.0, _var_var_326))
                else:
                    _var_var_256 = 1.0
                for _var_var_250 in (None, _var_var_246):
                    _var_var_62 = await _var_var_231.fetchone(
                        "SELECT physics_mae, ai_mae, lstm_mae, ridge_mae,\n                                  sample_count\n                           FROM group_method_performance\n                           WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                               AND season IS ?",
                        (_var_var_306, _var_var_242, _var_var_243, _var_var_250),
                    )
                    if _var_var_62 and _var_var_62[4] and (_var_var_62[4] > 0):
                        _var_var_327 = (
                            _var_var_245 * _var_var_247
                            + (1 - _var_var_245) * _var_var_62[0]
                        )
                        _var_var_328 = (
                            _var_var_245 * _var_var_249
                            + (1 - _var_var_245) * _var_var_62[1]
                            if _var_var_249 and _var_var_62[1]
                            else _var_var_249
                        )
                        _var_var_329 = (
                            _var_var_245 * _var_var_312
                            + (1 - _var_var_245) * _var_var_62[2]
                            if _var_var_312 and _var_var_62[2]
                            else _var_var_312
                        )
                        _var_var_330 = (
                            _var_var_245 * _var_var_313
                            + (1 - _var_var_245) * _var_var_62[3]
                            if _var_var_313 and _var_var_62[3]
                            else _var_var_313
                        )
                        _var_var_254 = _var_var_62[4] + len(_var_var_166["physics"])
                    else:
                        _var_var_327 = _var_var_247
                        _var_var_328 = _var_var_249
                        _var_var_329 = _var_var_312
                        _var_var_330 = _var_var_313
                        _var_var_254 = len(_var_var_166["physics"])
                    _var_var_331 = {"physics": _var_var_327}
                    if _var_var_328 is not None:
                        _var_var_331["ai"] = _var_var_328
                    if _var_var_329 is not None:
                        _var_var_331["lstm"] = _var_var_329
                    if _var_var_330 is not None:
                        _var_var_331["ridge"] = _var_var_330
                    _var_var_332 = min(_var_var_331, key=_var_var_331.get)
                    if _var_var_328 and _var_var_328 > 0 and (_var_var_327 > 0):
                        _var_var_333 = max(0.5, min(2.0, _var_var_327 / _var_var_328))
                    else:
                        _var_var_333 = 1.0
                    _var_var_257 = (
                        _var_var_306,
                        _var_var_242,
                        _var_var_243,
                        _var_var_250,
                        round(_var_var_327, 6),
                        round(_var_var_328, 6) if _var_var_328 else None,
                        round(_var_var_329, 6) if _var_var_329 else None,
                        round(_var_var_330, 6) if _var_var_330 else None,
                        None,
                        _var_var_332,
                        round(_var_var_333, 4),
                        _var_var_254,
                        now,
                    )
                    if _var_var_250 is None:
                        async with _var_var_231.transaction():
                            await _var_var_231.execute(
                                "DELETE FROM group_method_performance\n                                   WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                                       AND season IS NULL",
                                (_var_var_306, _var_var_242, _var_var_243),
                                auto_commit=False,
                            )
                            await _var_var_231.execute(
                                "INSERT INTO group_method_performance\n                                   (group_name, cloud_bucket, hour_bucket, season,\n                                    physics_mae, ai_mae, lstm_mae, ridge_mae, blend_mae,\n                                    best_method, ai_advantage_factor, sample_count, last_updated)\n                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                _var_var_257,
                                auto_commit=False,
                            )
                    else:
                        await _var_var_231.execute(
                            "INSERT INTO group_method_performance\n                               (group_name, cloud_bucket, hour_bucket, season,\n                                physics_mae, ai_mae, lstm_mae, ridge_mae, blend_mae,\n                                best_method, ai_advantage_factor, sample_count, last_updated)\n                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                               ON CONFLICT(group_name, cloud_bucket, hour_bucket, season) DO UPDATE SET\n                                   physics_mae = excluded.physics_mae,\n                                   ai_mae = excluded.ai_mae,\n                                   lstm_mae = excluded.lstm_mae,\n                                   ridge_mae = excluded.ridge_mae,\n                                   blend_mae = excluded.blend_mae,\n                                   best_method = excluded.best_method,\n                                   ai_advantage_factor = excluded.ai_advantage_factor,\n                                   sample_count = excluded.sample_count,\n                                   last_updated = excluded.last_updated",
                            _var_var_257,
                            auto_commit=True,
                        )
                    _var_var_311 += 1
                _LOGGER.info(
                    "Group method learning [%s/%s/%s]: physics=%.4f ai=%s lstm=%s ridge=%s best=%s advantage=%.2f (%d samples)",
                    _var_var_306,
                    _var_var_242,
                    _var_var_243,
                    _var_var_247,
                    f"{_var_var_249:.4f}" if _var_var_249 else "N/A",
                    f"{_var_var_312:.4f}" if _var_var_312 else "N/A",
                    f"{_var_var_313:.4f}" if _var_var_313 else "N/A",
                    _var_var_325,
                    _var_var_256,
                    _var_var_254,
                )
                continue
            _LOGGER.info(
                "Group method performance learning completed for %s: %d buckets updated",
                _var_var_25,
                _var_var_311,
            )
        except Exception as _var_var_2:
            _LOGGER.error(
                f"Group method performance learning error: {_var_var_2}", exc_info=True
            )
        return None if True else None

    async def _run_weather_expert_learning(self, now: datetime) -> None:
        """Learn weather expert weights from today's actual observations. @zara"""
        try:
            if not self.coordinator or not hasattr(
                self.coordinator, "weather_pipeline_manager"
            ):
                _LOGGER.debug(
                    "Weather expert learning skipped: no weather_pipeline_manager"
                )
                if True:
                    return
            _var_var_73 = self.coordinator.weather_pipeline_manager
            if not _var_var_73:
                _LOGGER.debug("Weather expert learning skipped: pipeline is None")
                if True:
                    return
            if not _var_var_73.weather_expert_learner:
                _LOGGER.debug(
                    "Weather expert learning skipped: learner not initialized"
                )
                if True:
                    return
            _var_var_25 = now.strftime("%Y-%m-%d")
            _var_var_99 = await _var_var_73.weather_expert_learner.learn_from_day(
                _var_var_25
            )
            if _var_var_99:
                _LOGGER.info(f"Weather expert learning SUCCESS for {_var_var_25}")
            else:
                _var_var_186 = "Learning failed or insufficient data"
                _LOGGER.debug(
                    f"Weather expert learning skipped for {_var_var_25}: {_var_var_186}"
                )
            try:
                _var_var_334 = await self.db.fetchall(
                    "SELECT prediction_id, target_hour, actual_kwh FROM hourly_predictions\n                       WHERE target_date = ?",
                    (_var_var_25,),
                )
                _var_var_335 = [
                    {
                        "prediction_id": _var_var_36[0],
                        "target_hour": _var_var_36[1],
                        "actual_kwh": _var_var_36[2],
                    }
                    for _var_var_36 in _var_var_334
                ]
                _var_var_336 = {}
                if _var_var_73.weather_actual_tracker:
                    _var_var_336 = await _var_var_73.weather_actual_tracker.get_daily_actual_weather(
                        _var_var_25
                    )
                if _var_var_335:
                    _var_var_337 = await _var_var_73.weather_expert_learner.learn_snow_prediction_accuracy(
                        _var_var_25, _var_var_335, _var_var_336
                    )
                    if _var_var_337.get("total_snow_predicted", 0) > 0:
                        _LOGGER.info(
                            f"Snow prediction learning for {_var_var_25}: {_var_var_337.get('snow_confirmed', 0)}/{_var_var_337.get('total_snow_predicted', 0)} confirmed ({_var_var_337.get('accuracy', 0):.0%} accuracy)"
                        )
            except Exception as _var_var_338:
                _LOGGER.debug(f"Snow prediction learning skipped: {_var_var_338}")
        except Exception as _var_var_2:
            _LOGGER.error(f"Weather expert learning error: {_var_var_2}", exc_info=True)
        if True:
            return None
