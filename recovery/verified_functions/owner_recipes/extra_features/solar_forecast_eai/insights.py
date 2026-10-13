# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
from collections import deque
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .setup_state import (
    DERIVED_ENERGY_TODAY_KEYS,
    cop_sink_kwargs,
    dhw_forecast_kwargs,
    generator_circuit_flow_implausible,
    hydraulics_setup_incomplete,
    is_false,
    is_true,
    normalize_operation_mode,
    power_watts,
)
from .daily_energy import (
    ENERGY_COUNTER_MODE_CUMULATIVE,
    DailyEnergyTracker,
    counter_rise_limit_kw,
    resolve_energy_counter_mode,
)
from .const import (
    ADVANCED_SENSORS,
    CONF_COP_RATED,
    CONF_DATA_QUALITY_TIER,
    CONF_CIRCULATION_PUMP_ENTITY,
    CONF_CIRCULATION_RETURN_TEMP_ENTITY,
    CONF_COMPRESSOR_ENTITY,
    CONF_DHW_TEMP_ENTITY,
    CONF_ELECTRICAL_MEASUREMENT_TOPOLOGY,
    CONF_FLOW_TEMP_ENTITY,
    CONF_HAS_DHW,
    CONF_HAS_HEATING_ELEMENT,
    CONF_HEAT_PUMP_ENABLED,
    CONF_HEATING_CAPACITY_KW,
    CONF_HEATING_ELEMENT_ENTITY,
    CONF_HEATING_ELEMENT_ENERGY_TODAY_ENTITY,
    CONF_HEATING_ELEMENT_POWER_ENTITY,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_JAZ_ENTITY,
    CONF_OPERATION_MODE_ENTITY,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_RETURN_TEMP_ENTITY,
    CONF_RUNTIME_COUNTER_SCOPE,
    CONF_RUNTIME_ENTITY,
    CONF_RUNTIME_DHW_ENTITY,
    CONF_RUNTIME_HEATING_ENTITY,
    CONF_SOURCE_TEMP_ENTITY,
    CONF_STARTS_COUNTER_SCOPE,
    CONF_STARTS_ENTITY,
    CONF_STORAGE_AMBIENT_TEMP_ENTITY,
    CONF_STORAGE_TEMP_ENTITY,
    CONF_STORAGE_VOLUME_L,
    CONF_TARGET_TEMP_ENTITY,
    CONF_THERMAL_ENERGY_ENTITY,
    CONF_VOLUME_FLOW_ENTITY,
    CONF_WALLBOX_ENABLED,
    CONF_WALLBOX_MAX_POWER_KW,
    CONF_WP_ENERGY_TODAY,
    CONF_WP_POWER_ENTITY,
    CONF_WP_TYPE,
    DEFAULT_WALLBOX_MAX_POWER_KW,
    COP_MODE_DHW,
    COP_MODE_HEATING,
    DEFAULT_COP_RATED,
    GENERATOR_FLOW_HINT_CLEAR_S,
    GENERATOR_FLOW_HINT_DE,
    GENERATOR_FLOW_HINT_EN,
    GENERATOR_FLOW_HINT_PERSIST_S,
    GENERATOR_FLOW_HYSTERESIS_K,
    GENERATOR_FLOW_THERMAL_REASON,
    GENERATOR_FLOW_TOLERANCE_K,
    DEFAULT_ELECTRICAL_MEASUREMENT_TOPOLOGY,
    DEFAULT_HEATING_CAPACITY_KW,
    DEFAULT_WP_TYPE,
    COUNTER_SCOPE_DAILY,
    COUNTER_SCOPE_LIFETIME,
    COUNTER_SCOPE_UNKNOWN,
    ELECTRICAL_TOPOLOGY_LEGACY_AGGREGATE,
    ELECTRICAL_TOPOLOGY_SEPARATE,
    REQUIRED_SENSORS,
    SINK_ORIGIN_UNCONFIGURED,
    STANDARD_SENSORS,
)
from .energy_context import EAISharedEnergyAllocator, WallboxEnvelope
from .mobility import EAIMobilityPlanner
from .assignment_policy import is_allowed_shared_assignment
from .thermal_loss import thermal_loss_snapshot


class EAIInsightsEngine:

    def _last_valid_operating_metrics(self, now: datetime) -> dict[str, Any] | None:
        for _var_var_10 in reversed(self._samples):
            _var_var_106 = _var_var_10.get("thermal_power_kw")
            _var_var_14 = _var_var_10.get("timestamp")
            _var_var_40 = _var_var_10.get("power_w")
            if not isinstance(_var_var_106, (int, float)) or _var_var_106 <= 0:
                continue
            if (
                isinstance(_var_var_40, (int, float))
                and _var_var_40 <= _MIN_COP_ELECTRICAL_POWER_W
            ):
                continue
            if not isinstance(_var_var_14, datetime):
                continue
            _var_var_107 = (
                _var_var_14
                if _var_var_14.tzinfo is not None
                else _var_var_14.replace(tzinfo=timezone.utc)
            )
            if now - _var_var_107 > _LAST_VALID_MAX_AGE:
                continue
            _var_var_108 = None
            if (
                isinstance(_var_var_40, (int, float))
                and _var_var_40 > _MIN_COP_ELECTRICAL_POWER_W
            ):
                _var_var_89 = _var_var_106 / (_var_var_40 / 1000.0)
                if 0.5 <= _var_var_89 <= 12:
                    _var_var_108 = _var_var_89
            return {
                "thermal_power_kw": _var_var_106,
                "cop": _var_var_108,
                "timestamp": _var_var_14,
            }

    def _forecast(
        self, readings: dict[str, dict[str, Any]], now: datetime
    ) -> dict[str, Any]:
        _var_var_135 = self._temperature_c(readings[CONF_OUTDOOR_TEMP_ENTITY])
        _var_var_136 = self._temperature_c(readings[CONF_TARGET_TEMP_ENTITY]) or 21.0
        _var_var_137 = is_true(self._config, CONF_HAS_DHW)
        _var_var_138 = [
            _var_var_100
            for _var_var_100 in self._coordinator_predictions()
            if self._forecast_point_hour(_var_var_100) is not None
        ]
        _var_var_139 = [
            _var_var_100
            for _var_var_100 in self._sfml_predictions()
            if self._forecast_point_hour(_var_var_100) is not None
        ]
        _var_var_140 = self._config.get(CONF_HEAT_PUMP_ENABLED, True) is True
        _var_var_141 = _var_var_140 and bool(_var_var_138)
        _var_var_142 = _var_var_138 if _var_var_141 else _var_var_139
        _var_var_143 = (
            "persistent_eai_coordinator"
            if _var_var_141
            else (
                "sfml_energy_context_only"
                if not _var_var_140
                else "explainable_heat_demand_model"
            )
        )
        _var_var_144 = self._weather_temperature_by_hour()
        _var_var_145 = self._weather_temperature_sources
        _var_var_146 = (
            self._forecast_calibrator() if _var_var_140 and (not _var_var_141) else None
        )
        _var_var_147 = self._learned_u_factor(_var_var_146)
        _var_var_148 = (
            self._blocked_space_heating_days(
                _var_var_142, _var_var_144, calibrator=_var_var_146
            )
            if _var_var_140 and (not _var_var_141)
            else set()
        )
        _var_var_149 = {
            self._point_key(_var_var_100): _var_var_100.get("prediction_kwh")
            for _var_var_100 in _var_var_139
        }
        from .thermodynamics.thermodynamics_engine import (
            local_calendar_date as _var_var_150,
        )

        _var_var_151 = []
        for _var_var_152, _var_var_100 in enumerate(_var_var_142):
            _var_var_153 = self._forecast_point_hour(_var_var_100)
            if _var_var_153 is None:
                continue
            _var_var_14 = (
                _var_var_100.get("target_datetime") or _var_var_153.isoformat()
            )
            _var_var_154 = self._forecast_temperature(_var_var_100)
            _var_var_155 = (
                "load_forecast" if _var_var_154 is not None else "forecast_temperature"
            )
            if _var_var_154 is None:
                _var_var_154 = _var_var_144.get(_var_var_153)
                if _var_var_154 is not None:
                    _var_var_155 = _var_var_145.get(
                        _var_var_153, "forecast_temperature"
                    )
            _var_var_156 = (
                "forecast" if _var_var_154 is not None else "current_fallback"
            )
            if _var_var_154 is None:
                _var_var_155 = "current_sensor_fallback"
            _var_var_154 = _var_var_135 if _var_var_154 is None else _var_var_154
            if _var_var_154 is None:
                if _var_var_140:
                    continue
            _var_var_157 = _var_var_153.astimezone(self._local_timezone()).hour
            if _var_var_141:
                _var_var_158 = _var_var_100.get(
                    "prediction_kwh", _var_var_100.get("predicted_kwh")
                )
                if (
                    isinstance(_var_var_158, bool)
                    or not isinstance(_var_var_158, (int, float))
                    or (not isfinite(_var_var_158))
                    or (not 0 <= float(_var_var_158) <= 1000)
                ):
                    continue
                _var_var_159 = max(0.0, float(_var_var_158))
                _var_var_160 = _var_var_149.get(self._point_key(_var_var_100))
            elif _var_var_140:
                _var_var_119 = self._modeled_hourly_consumption(
                    readings,
                    _var_var_154,
                    _var_var_157,
                    _var_var_136,
                    suppress_space_heating=_var_var_150(
                        _var_var_153, self._local_timezone()
                    )
                    in _var_var_148,
                    learned_u_factor=_var_var_147,
                    now=now,
                )
                if _var_var_119 is None:
                    continue
                _var_var_159 = max(0.0, _var_var_119.electrical_consumption_kw)
                if _var_var_146 is not None:
                    _var_var_161 = (
                        _var_var_150(_var_var_153, self._local_timezone())
                        in _var_var_148
                    )
                    _var_var_162 = _var_var_137 and (
                        _var_var_161 or _var_var_119.heating_demand_kw <= 0.01
                    )
                    try:
                        _var_var_163 = _var_var_146.get_correction_factor(
                            float(_var_var_154),
                            0.0,
                            _var_var_157,
                            is_dhw_only=_var_var_162,
                        )
                        if (
                            _var_var_163 is not None
                            and float(_var_var_163.confidence) > 0.3
                        ):
                            _var_var_159 *= float(_var_var_163.correction_factor)
                    except Exception:
                        pass
                    _var_var_159 = max(0.0, _var_var_159)
                _var_var_164 = (
                    _var_var_119.electrical_consumption_kw
                    if _var_var_119.heating_demand_kw <= 0.01
                    and _var_var_119.is_heating_needed
                    else 0.0
                )
                _var_var_160 = _var_var_100.get("prediction_kwh")
            else:
                _var_var_156 = "not_required"
                _var_var_155 = "not_applicable"
                _var_var_164 = 0.0
                _var_var_159 = 0.0
                _var_var_160 = _var_var_100.get("prediction_kwh")
            (
                _var_var_165,
                _var_var_166,
                _var_var_167,
                _var_var_168,
                _var_var_169,
                _var_var_170,
            ) = self._prediction_interval(
                _var_var_100,
                _var_var_159,
                coordinator_source=_var_var_141,
                temperature_origin=_var_var_156,
                horizon_hours=_var_var_152,
            )
            _var_var_151.append(
                {
                    "timestamp": _var_var_14 or now.isoformat(),
                    "outdoor_c": _round(_var_var_154, 1),
                    "forecast_kw": _round(_var_var_159),
                    "lower_kw": _round(_var_var_165),
                    "upper_kw": _round(_var_var_166),
                    "uncertainty_percent": _var_var_167,
                    "confidence_percent": _var_var_168,
                    "uncertainty_drivers": _var_var_169,
                    "interval_method": _var_var_170,
                    "actual_kw": _round(
                        self._completed_actual_kw(_var_var_100, _var_var_153, now)
                    ),
                    "pv_forecast_kwh": (
                        _round(float(_var_var_160))
                        if not isinstance(_var_var_160, bool)
                        and isinstance(_var_var_160, (int, float))
                        and isfinite(float(_var_var_160))
                        and (0 <= float(_var_var_160) <= 1000)
                        else None
                    ),
                    "temperature_origin": _var_var_156,
                    "temperature_source": _var_var_155,
                    "mode": (
                        _var_var_100.get("mode", "heating")
                        if _var_var_141
                        else (
                            "standby"
                            if not _var_var_140
                            else "dhw" if _var_var_164 else "heating"
                        )
                    ),
                }
            )
            continue
        _var_var_151 = self._energy_allocator.enrich(
            _var_var_151, self._wallbox_envelope()
        )
        _var_var_171 = (
            sum(
                (
                    _var_var_100["temperature_origin"] == "forecast"
                    for _var_var_100 in _var_var_151
                )
            )
            if _var_var_140
            else 0
        )
        _var_var_172 = len(_var_var_151) - _var_var_171 if _var_var_140 else 0
        _var_var_173 = (
            sum(
                (
                    _var_var_100["temperature_source"] == "weather_intelligence_stale"
                    for _var_var_100 in _var_var_151
                )
            )
            if _var_var_140
            else 0
        )
        _var_var_174 = (
            "ready"
            if _var_var_151 and (not _var_var_140 or _var_var_172 == 0)
            else (
                "partial"
                if _var_var_171
                else "estimated" if _var_var_151 else "unavailable"
            )
        )
        _var_var_175 = (
            round(
                sum(
                    (
                        _var_var_100["uncertainty_percent"]
                        for _var_var_100 in _var_var_151
                    )
                )
                / len(_var_var_151)
            )
            if _var_var_151
            else None
        )
        _var_var_176 = (
            round(
                sum(
                    (
                        _var_var_100["confidence_percent"]
                        for _var_var_100 in _var_var_151
                    )
                )
                / len(_var_var_151)
            )
            if _var_var_151
            else None
        )
        return {
            "available": bool(_var_var_151),
            "locked": False,
            "hours": _var_var_151,
            "run_status": _var_var_174,
            "quality": _var_var_174,
            "temperature_forecast_hours": _var_var_171,
            "temperature_fallback_hours": _var_var_172,
            "temperature_stale_forecast_hours": _var_var_173,
            "temperature_forecast_coverage_percent": (
                _round(_var_var_171 / len(_var_var_151) * 100, 0)
                if _var_var_151 and _var_var_140
                else None
            ),
            "model_origin": _var_var_143,
            "main_drivers": (
                ["SFML-Solarprognose", "STATS-Haushaltsprofil"]
                if not _var_var_140
                else [
                    "Außentemperatur",
                    "Nenn-Heizleistung",
                    "Nenn-COP",
                    "Tagesprofil",
                    "Warmwasserzyklus" if _var_var_137 else "Raumheizung",
                ]
            ),
            "uncertainty": {
                "available": bool(_var_var_151),
                "average_percent": _var_var_175,
                "confidence_percent": _var_var_176,
                "method": "data_quality_model_source_temperature_and_horizon",
                "explanation": "Das Band wird mit Modellherkunft, Datenqualität, Temperaturquelle und Prognosehorizont breiter oder schmaler.",
                "lower_field": "lower_kw",
                "upper_field": "upper_kw",
            },
            "optimization": self._optimization(_var_var_151),
        }

    def _mode_segmented_cop(self) -> dict[str, Any]:
        _var_var_258 = {
            "heating": {"thermal": 0.0, "electric": 0.0, "samples": 0},
            "dhw": {"thermal": 0.0, "electric": 0.0, "samples": 0},
        }
        for _var_var_10 in self._samples:
            if _var_var_10.get("compressor_on") is False:
                continue
            _var_var_106 = _var_var_10.get("thermal_power_kw")
            _var_var_40 = _var_var_10.get("power_w")
            if (
                not isinstance(_var_var_106, (int, float))
                or not isinstance(_var_var_40, (int, float))
                or (not not _var_var_40 <= _MIN_COP_ELECTRICAL_POWER_W)
                or (_var_var_106 <= 0)
            ):
                continue
            _var_var_37 = _var_var_10.get("operation_mode")
            if _var_var_37 not in _var_var_258:
                continue
            _var_var_259 = _var_var_40 / 1000.0
            if _var_var_259 <= 0:
                continue
            _var_var_260 = _var_var_106 / _var_var_259
            if not 0.5 <= _var_var_260 <= 12:
                continue
            _var_var_258[_var_var_37]["thermal"] += _var_var_106
            _var_var_258[_var_var_37]["electric"] += _var_var_259
            _var_var_258[_var_var_37]["samples"] += 1
            continue
        _var_var_119 = {
            "heating": None,
            "dhw": None,
            "heating_samples": 0,
            "dhw_samples": 0,
            "heating_reason": None,
            "dhw_reason": None,
        }
        for _var_var_37 in ("heating", "dhw"):
            _var_var_214 = _var_var_258[_var_var_37]["samples"]
            _var_var_119[f"{_var_var_37}_samples"] = _var_var_214
            _var_var_261 = _var_var_258[_var_var_37]["electric"]
            if _var_var_214 < 6 or _var_var_261 <= 0:
                _var_var_119[f"{_var_var_37}_reason"] = "insufficient_samples"
            else:
                _var_var_119[_var_var_37] = (
                    _var_var_258[_var_var_37]["thermal"] / _var_var_261
                )
        return _var_var_119

    @staticmethod
    def _forecast_energy_for_day(
        points: list[dict[str, Any]], target_day: Any, local_timezone: Any
    ) -> float:
        _var_var_359 = datetime.combine(
            target_day, datetime.min.time(), tzinfo=local_timezone
        )
        _var_var_360 = _var_var_359 + timedelta(days=1)
        _var_var_297 = 0.0
        for _var_var_100 in points:
            try:
                _var_var_361 = datetime.fromisoformat(
                    str(_var_var_100["interval_start"]).replace("Z", "+00:00")
                )
                _var_var_362 = datetime.fromisoformat(
                    str(_var_var_100["interval_end"]).replace("Z", "+00:00")
                )
            except (KeyError, TypeError, ValueError):
                continue
            if _var_var_361.tzinfo is None:
                _var_var_361 = _var_var_361.replace(tzinfo=timezone.utc)
            if _var_var_362.tzinfo is None:
                _var_var_362 = _var_var_362.replace(tzinfo=timezone.utc)
            _var_var_363 = max(_var_var_361, _var_var_359)
            _var_var_364 = min(_var_var_362, _var_var_360)
            _var_var_365 = (_var_var_364 - _var_var_363).total_seconds() / 3600
            _var_var_182 = (_var_var_362 - _var_var_361).total_seconds() / 3600
            if _var_var_365 <= 0 or _var_var_182 <= 0:
                continue
            _var_var_366 = _var_var_100.get("heat_pump_kwh")
            if isinstance(_var_var_366, (int, float)) and isfinite(_var_var_366):
                _var_var_297 += (
                    max(0.0, float(_var_var_366)) * _var_var_365 / _var_var_182
                )
                continue
            _var_var_39 = _var_var_100.get("forecast_kw")
            if not isinstance(_var_var_39, (int, float)):
                continue
            if not isfinite(_var_var_39):
                continue
            _var_var_297 += max(0.0, float(_var_var_39)) * _var_var_365
        return _var_var_297
