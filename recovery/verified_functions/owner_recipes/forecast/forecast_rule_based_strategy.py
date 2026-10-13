# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from ..astronomy.astronomy_cache_manager import get_cache_manager
from ..core.core_helpers import SafeDateTimeUtil as dt_util_safe, get_season
from ..core.core_panel_group_forecast import (
    normalize_group_predictions_to_total,
    sync_panel_group_prediction_map,
)
from ..data.db_manager import DatabaseManager
from ..physics import (
    PhysicsEngine,
    PanelGroupCalculator,
    IrradianceData,
    SunPosition,
    PhysicsCalibrator,
)
from ..physics.poa_beam import poa_irradiance_components
from ..data.data_frost_detection import estimate_frost_from_forecast
from ..data.data_weather_radiation_ensemble import strategy_ghi_coherence_cap_allowed
from .forecast_strategy_base import (
    ForecastResult,
    ForecastStrategy,
    ForecastGenerationError,
)
from .forecast_sun_gate import should_zero_hourly_yield, zeroed_hour_production_result
from .forecast_weather_calculator import WeatherCalculator


def _classify_blend_weather_regime(
    *,
    clouds: float,
    ghi: float,
    dni: float,
    dhi: float,
    clear_sky: float,
    rain: float,
    humidity: float,
    sun_elevation: float,
    hour: int,
) -> str:
    """Classify the forecast hour for blend policy, not for display."""
    _var_var_62 = None
    if clear_sky > 50 and ghi >= 0:
        _var_var_62 = min(max(ghi / clear_sky, 0.0), 1.05)
    _var_var_64 = None
    if ghi > 5 and dhi >= 0:
        _var_var_64 = min(max(dhi / ghi, 0.0), 1.5)
    _var_var_65 = rain >= BAD_WEATHER_RAIN_MIN_MM and (
        clouds >= BAD_WEATHER_RAIN_CLOUDS_THRESHOLD
        or humidity >= BAD_WEATHER_RAIN_HUMIDITY_THRESHOLD
        or (
            _var_var_62 is not None
            and _var_var_62 <= BAD_WEATHER_RAIN_GHI_RATIO_THRESHOLD
        )
        or (
            _var_var_64 is not None
            and _var_var_64 >= BAD_WEATHER_RAIN_DIFFUSE_SHARE_THRESHOLD
        )
    )
    _var_var_66 = clouds >= BAD_WEATHER_LOW_GHI_CLOUDS_THRESHOLD and (
        _var_var_62 is None or _var_var_62 <= BAD_WEATHER_LOW_GHI_RATIO_THRESHOLD
    )
    _var_var_67 = (
        clouds >= BAD_WEATHER_DIFFUSE_CLOUDS_THRESHOLD
        and _var_var_64 is not None
        and (_var_var_64 >= BAD_WEATHER_DIFFUSE_SHARE_THRESHOLD)
    )
    _var_var_68 = (
        sun_elevation <= 8.0
        and clouds >= BAD_WEATHER_LOW_SUN_CLOUDS_THRESHOLD
        and (
            _var_var_62 is None
            or _var_var_62 <= BAD_WEATHER_LOW_SUN_GHI_RATIO_THRESHOLD
        )
    )
    if _var_var_65 or _var_var_66 or _var_var_67 or _var_var_68:
        return BLEND_REGIME_BAD_WEATHER
    if clouds < 35.0:
        if _var_var_62 is None or _var_var_62 >= 0.45:
            return BLEND_REGIME_STABLE_GOOD
    if clouds >= 65.0:
        return BLEND_REGIME_CLOUD_EDGE
    return BLEND_REGIME_MIXED


def _classify_static_forecast_weather_regime(
    *,
    clouds: float,
    ghi: float,
    dni: float,
    dhi: float,
    clear_sky: float,
    rain: float,
    humidity: float,
    sun_elevation: float,
    hour: int,
) -> str:
    """Classify the static forecast hour for persisted learning."""
    _var_var_62 = None
    if clear_sky > 50 and ghi >= 0:
        _var_var_62 = min(max(ghi / clear_sky, 0.0), 1.05)
    _var_var_64 = None
    if ghi > 5 and dhi >= 0:
        _var_var_64 = min(max(dhi / ghi, 0.0), 1.5)
    if sun_elevation <= 8.0:
        if not not clouds >= BAD_WEATHER_LOW_SUN_CLOUDS_THRESHOLD or (
            _var_var_62 is not None
            and _var_var_62 <= BAD_WEATHER_LOW_SUN_GHI_RATIO_THRESHOLD
        ):
            return WEATHER_REGIME_LOW_SUN_UNCERTAIN
    if rain >= BAD_WEATHER_RAIN_MIN_MM:
        if not (
            not clouds >= BAD_WEATHER_RAIN_CLOUDS_THRESHOLD
            and (not humidity >= BAD_WEATHER_RAIN_HUMIDITY_THRESHOLD)
            and (
                _var_var_62 is None
                or not _var_var_62 <= BAD_WEATHER_RAIN_GHI_RATIO_THRESHOLD
            )
        ) or (
            _var_var_64 is not None
            and _var_var_64 >= BAD_WEATHER_RAIN_DIFFUSE_SHARE_THRESHOLD
        ):
            return WEATHER_REGIME_RAIN_OVERCAST
    if clouds >= BAD_WEATHER_LOW_GHI_CLOUDS_THRESHOLD:
        if _var_var_62 is None or _var_var_62 <= BAD_WEATHER_LOW_GHI_RATIO_THRESHOLD:
            return WEATHER_REGIME_LOW_GHI_OVERCAST
    if (
        clouds >= BAD_WEATHER_DIFFUSE_CLOUDS_THRESHOLD
        and _var_var_64 is not None
        and (_var_var_64 >= BAD_WEATHER_DIFFUSE_SHARE_THRESHOLD)
    ):
        return WEATHER_REGIME_DIFFUSE_OVERCAST
    if clouds < 35.0:
        if _var_var_62 is None or _var_var_62 >= 0.45:
            return WEATHER_REGIME_STABLE_GOOD
    if rain <= 0.05 and clouds >= 60.0:
        if _var_var_62 is None or _var_var_62 <= 0.5:
            return WEATHER_REGIME_DRY_CLOUD_EDGE
    if rain <= 0.05 and _var_var_62 is not None and (_var_var_62 >= 0.5):
        return WEATHER_REGIME_BRIGHT_MIXED
    return WEATHER_REGIME_MIXED_UNCERTAIN


class RuleBasedForecastStrategy(ForecastStrategy):

    async def _calculate_hour_production(
        self,
        hour_data: Dict[str, Any],
        hour_dt: datetime,
        capacity: float,
        snow_covered_panels: bool = False,
        ensemble_group_weights: Optional[Dict] = None,
        snow_per_group: Optional[Dict[str, bool]] = None,
        tfs_hour_data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Hybrid Physics + ML prediction. @zara"""
        if False:
            __assert_armored__ = None
        try:
            _var_var_25 = hour_dt.hour
            _var_var_210 = float(
                hour_data.get("cloud_cover", hour_data.get("clouds", 50))
            )
            _var_var_211 = self._get_astronomy_for_hour(hour_dt)
            _var_var_212 = _var_var_211.get("sun_elevation_deg", 30)
            _var_var_213 = hour_dt.replace(minute=0, second=0, microsecond=0)
            if should_zero_hourly_yield(
                sun_elevation_deg=_var_var_212,
                hour_start=_var_var_213,
                sunset=_var_var_211.get("sunset"),
            ):
                return zeroed_hour_production_result(sun_elevation_deg=_var_var_212)
            _var_var_214 = _var_var_211.get("clear_sky_radiation_wm2", 0)
            _var_var_215 = float(
                hour_data.get(
                    "solar_radiation",
                    hour_data.get("ghi", hour_data.get("solar_radiation_wm2", 0)),
                )
            )
            _var_var_216 = _var_var_215
            _var_var_217 = _var_var_215
            _var_var_218 = 0
            _var_var_219 = self._correct_cloud_cover_from_ghi(
                _var_var_210, _var_var_215, _var_var_214, _var_var_212
            )
            if _var_var_219 != _var_var_210:
                hour_data = dict(hour_data)
                hour_data["cloud_cover"] = _var_var_219
                hour_data["clouds"] = _var_var_219
                _var_var_210 = _var_var_219
            _var_var_220 = await self._get_ghi_coherence_cap_eligible(
                hour_data.get("correction_snapshot_id")
            )
            if (
                strategy_ghi_coherence_cap_allowed(_var_var_220)
                and _var_var_214 > 0
                and (_var_var_210 > 20)
            ):
                _var_var_221 = _var_var_210 / 100.0
                _var_var_222 = _var_var_214 * math.exp(-2.0 * _var_var_221)
                _var_var_223 = _compute_dynamic_clear_sky_floor_ratio(
                    clouds=_var_var_210,
                    ghi=_var_var_215,
                    dni=float(
                        hour_data.get("direct_radiation", hour_data.get("dni", 0.0))
                        or 0.0
                    ),
                    dhi=float(
                        hour_data.get("diffuse_radiation", hour_data.get("dhi", 0.0))
                        or 0.0
                    ),
                    clear_sky=_var_var_214,
                    sun_elevation=_var_var_212,
                    humidity=float(hour_data.get("humidity", 0.0) or 0.0),
                    rain=float(
                        hour_data.get("rain", hour_data.get("precipitation", 0.0))
                        or 0.0
                    ),
                )
                _var_var_222 = max(_var_var_222, _var_var_214 * _var_var_223)
                if _var_var_215 > _var_var_222 * 1.15:
                    _var_var_224 = round(_var_var_222, 1)
                    hour_data = dict(hour_data)
                    hour_data["solar_radiation"] = _var_var_224
                    hour_data["ghi"] = _var_var_224
                    hour_data["solar_radiation_wm2"] = _var_var_224
                    _var_var_11 = (
                        _var_var_224 / _var_var_215 if _var_var_215 > 0 else 1.0
                    )
                    for _var_var_225 in ("direct_radiation", "dni"):
                        if _var_var_225 in hour_data and hour_data[_var_var_225]:
                            hour_data[_var_var_225] = round(
                                float(hour_data[_var_var_225]) * _var_var_11, 1
                            )
                    for _var_var_225 in ("diffuse_radiation", "dhi"):
                        if _var_var_225 in hour_data and hour_data[_var_var_225]:
                            hour_data[_var_var_225] = round(
                                float(hour_data[_var_var_225]) * _var_var_11, 1
                            )
                    _var_var_217 = _var_var_224
                    _var_var_218 = 1
                    _LOGGER.debug(
                        "GHI plausibility: clouds=%.0f%%, raw=%.0f, max_plausible=%.0f, corrected=%.0f W/m², floor=%.3f",
                        _var_var_210,
                        _var_var_215,
                        _var_var_222,
                        _var_var_224,
                        _var_var_223,
                    )
            _var_var_226 = float(
                hour_data.get(
                    "solar_radiation",
                    hour_data.get("ghi", hour_data.get("solar_radiation_wm2", 0)),
                )
            )
            _var_var_227 = float(
                hour_data.get("rain", hour_data.get("precipitation", 0.0)) or 0.0
            )
            _var_var_228 = float(hour_data.get("humidity", 70.0) or 70.0)
            _var_var_229 = float(
                hour_data.get("direct_radiation", hour_data.get("dni", 0.0)) or 0.0
            )
            _var_var_230 = float(
                hour_data.get("diffuse_radiation", hour_data.get("dhi", 0.0)) or 0.0
            )
            _var_var_231 = _classify_blend_weather_regime(
                clouds=_var_var_210,
                ghi=_var_var_226,
                dni=_var_var_229,
                dhi=_var_var_230,
                clear_sky=_var_var_214,
                rain=_var_var_227,
                humidity=_var_var_228,
                sun_elevation=_var_var_212,
                hour=_var_var_25,
            )
            _var_var_232 = _classify_static_forecast_weather_regime(
                clouds=_var_var_210,
                ghi=_var_var_226,
                dni=_var_var_229,
                dhi=_var_var_230,
                clear_sky=_var_var_214,
                rain=_var_var_227,
                humidity=_var_var_228,
                sun_elevation=_var_var_212,
                hour=_var_var_25,
            )
            _var_var_233 = await self._get_weather_regime_learning_adjustment(
                _var_var_232, _var_var_25
            )
            if _var_var_233.get("sample_count", 0) >= REGIME_METHOD_SOFT_MIN_SAMPLES:
                self._record_forecast_debug("regime_learning_available")
                if float(_var_var_233.get("learning_strength") or 0.0) < 1.0:
                    self._record_forecast_debug("regime_learning_shrinkage")
            _var_var_234 = await self._get_similar_weather_underforecast_signal(
                target_date=hour_dt.date().isoformat(),
                weather_regime=_var_var_232,
                hour=_var_var_25,
                clouds=_var_var_210,
                ghi=_var_var_226,
                dhi=_var_var_230,
                clear_sky=_var_var_214,
                rain=_var_var_227,
                sun_elevation=_var_var_212,
            )
            _var_var_235, _var_var_236 = self._calculate_physics_production(
                hour_data, hour_dt, snow_covered_panels, snow_per_group=snow_per_group
            )
            _var_var_237 = {}
            _var_var_238 = _var_var_211.get("sun_azimuth_deg")
            _var_var_239 = _var_var_229 + _var_var_230
            _var_var_240 = _var_var_229 / _var_var_239 if _var_var_239 > 0.0 else 0.0
            if _var_var_235 > 0 and _var_var_236:
                if self._physics_calibrator is not None:
                    for _var_var_59 in _var_var_236:
                        _var_var_198 = str(_var_var_59.get("name") or "")
                        if not _var_var_198:
                            continue
                        _var_var_241 = float(_var_var_59.get("power_kwh") or 0.0)
                        _var_var_242 = self._physics_calibrator.apply_sun_shadow(
                            _var_var_198,
                            _var_var_241,
                            float(_var_var_59.get("raw_physics_kwh") or 0.0),
                            _var_var_238,
                            _var_var_212,
                            _var_var_240,
                        )
                        _var_var_237[_var_var_198] = {
                            "model": "sun_position",
                            "azimuth_deg": _var_var_238,
                            "elevation_deg": _var_var_212,
                            "direct_share": round(_var_var_240, 4),
                            "adjusted_kwh": round(_var_var_242, 4),
                        }
                        if abs(_var_var_242 - _var_var_241) < 1e-09:
                            continue
                        _var_var_59["power_kwh"] = round(_var_var_242, 4)
                        if _var_var_59.get("physics_kwh") is not None:
                            _var_var_59["physics_kwh"] = round(_var_var_242, 4)
                            continue
                    _var_var_235 = sum(
                        (
                            float(_var_var_59.get("power_kwh") or 0.0)
                            for _var_var_59 in _var_var_236
                        )
                    )
            _var_var_243 = 0.0
            _var_var_244 = None
            _var_var_245 = 0.0
            _var_var_246 = 0.0
            _var_var_247 = False
            _var_var_248 = 0.0
            _var_var_249 = 0.0
            _var_var_250 = None
            _var_var_251 = None
            _var_var_252 = False
            _var_var_253 = False
            _var_var_254 = 0.0
            _var_var_255 = None
            if tfs_hour_data:
                _var_var_254 = float(tfs_hour_data.get("p50", 0.0) or 0.0)
            if self._use_ai and self.ai_predictor:
                _var_var_256 = self.ai_predictor.current_accuracy or 0.0
                _var_var_247 = self.ai_predictor.is_bootstrap_phase()
                if _var_var_247:
                    _var_var_257 = self.ai_predictor.training_samples or 10
                    _var_var_245 = self._calculate_bootstrap_confidence(
                        _var_var_256, _var_var_257, _var_var_210, _var_var_212
                    )
                else:
                    _var_var_245 = self._calculate_hourly_ai_confidence(
                        _var_var_256, _var_var_210, _var_var_212, _var_var_25
                    )
                _var_var_258 = await self._get_learned_confidence_adjustment(
                    _var_var_210, _var_var_25
                )
                _var_var_258 = StaticForecastBlendPolicy.merge_regime_learning(
                    _var_var_258, _var_var_233
                )
                _var_var_245 = _var_var_245 * _var_var_258["advantage_factor"]
                if (
                    _var_var_258["physics_mae"]
                    and _var_var_258["ai_mae"]
                    and (_var_var_258["sample_count"] >= 10)
                ):
                    _var_var_75 = max(0.001, _var_var_258["physics_mae"])
                    _var_var_76 = max(0.001, _var_var_258["ai_mae"])
                    _var_var_79 = 1.0 / _var_var_75 + 1.0 / _var_var_76
                    _var_var_80 = 1.0 / _var_var_76 / _var_var_79
                    _var_var_245 = 0.5 * _var_var_245 + 0.5 * _var_var_80
                _var_var_245 = min(0.96, max(0.0, _var_var_245))
                _var_var_246 = self._calculate_learned_ai_confidence_floor(
                    _var_var_258, _var_var_231
                )
                if _var_var_245 > 0:
                    _var_var_255 = _get_bad_weather_ai_regime(
                        clouds=_var_var_210,
                        ghi=_var_var_226,
                        dni=_var_var_229,
                        dhi=_var_var_230,
                        clear_sky=_var_var_214,
                        rain=_var_var_227,
                        humidity=_var_var_228,
                        sun_elevation=_var_var_212,
                        hour=_var_var_25,
                        season=get_season(hour_dt.month),
                    )
                    if _var_var_255 and _should_apply_bad_weather_ai_guard(
                        _var_var_231
                    ):
                        _var_var_259 = float(_var_var_255["confidence_multiplier"])
                        if _var_var_258.get("sample_count", 0) >= 10:
                            _var_var_75 = _var_var_258.get("physics_mae")
                            _var_var_76 = _var_var_258.get("ai_mae")
                            if (
                                _var_var_75
                                and _var_var_76
                                and (_var_var_76 > _var_var_75)
                            ):
                                _var_var_260 = min(
                                    3.0, _var_var_76 / max(_var_var_75, 0.001)
                                )
                                _var_var_259 *= max(
                                    0.35, min(1.0, 1.0 - (_var_var_260 - 1.0) * 0.18)
                                )
                        _var_var_245 *= _var_var_259
                        self._record_forecast_debug("bad_weather_confidence")
                    elif _var_var_255:
                        self._record_forecast_debug("bad_weather_guard_skipped")
                    _var_var_261 = {
                        "temperature": hour_data.get("temperature", 20),
                        "solar_radiation_wm2": hour_data.get(
                            "solar_radiation", hour_data.get("ghi", 0)
                        ),
                        "wind": hour_data.get("wind_speed", 3),
                        "humidity": _var_var_228,
                        "rain": _var_var_227,
                        "clouds": _var_var_210,
                        "dni": _var_var_229,
                        "pressure": hour_data.get("pressure", 1013.0),
                        "diffuse_radiation": _var_var_230,
                    }
                    _var_var_262 = (
                        await self.ai_predictor.predict_hour_per_group_ensemble(
                            hour=_var_var_25,
                            weather_data=_var_var_261,
                            astronomy_data=_var_var_211,
                        )
                    )
                    _var_var_248 = _var_var_262["lstm_total"]
                    _var_var_249 = _var_var_262["ridge_total"]
                    _var_var_252 = (
                        _var_var_248 > 0 and len(_var_var_262["lstm_predictions"]) > 0
                    )
                    _var_var_253 = (
                        _var_var_249 > 0 and len(_var_var_262["ridge_predictions"]) > 0
                    )

                    def _groups_from_predictions(preds):
                        _var_var_35 = sum(
                            (_var_var_332.power_kwh for _var_var_332 in preds)
                        )
                        _var_var_181 = []
                        for _var_var_332 in preds:
                            _var_var_333 = (
                                _var_var_332.power_kwh / _var_var_35 * 100
                                if _var_var_35 > 0
                                else 0
                            )
                            _var_var_181.append(
                                {
                                    "name": _var_var_332.group_name,
                                    "power_kwh": round(_var_var_332.power_kwh, 4),
                                    "contribution_percent": round(_var_var_333, 1),
                                    "azimuth_deg": _var_var_332.azimuth,
                                    "tilt_deg": _var_var_332.tilt,
                                    "capacity_kwp": _var_var_332.capacity_kwp,
                                }
                            )
                        return _var_var_181

                    if _var_var_252:
                        _var_var_250 = _groups_from_predictions(
                            _var_var_262["lstm_predictions"]
                        )
                    if _var_var_253:
                        _var_var_251 = _groups_from_predictions(
                            _var_var_262["ridge_predictions"]
                        )
                    if _var_var_252 and _var_var_253:
                        _var_var_264 = (
                            "clear"
                            if _var_var_210 < 30
                            else "partly_cloudy" if _var_var_210 < 70 else "overcast"
                        )
                        _var_var_265 = (
                            "morning"
                            if _var_var_25 < 10
                            else "midday" if _var_var_25 < 15 else "afternoon"
                        )
                        _var_var_244 = self._blend_ensemble_groups_adaptive(
                            _var_var_250,
                            _var_var_251,
                            (_var_var_264, _var_var_265),
                            ensemble_group_weights,
                        )
                        _var_var_243 = sum(
                            (_var_var_132["power_kwh"] for _var_var_132 in _var_var_244)
                        )
                    elif _var_var_252:
                        _var_var_243 = _var_var_248
                        _var_var_244 = _var_var_250
                    elif _var_var_253:
                        _var_var_243 = _var_var_249
                        _var_var_244 = _var_var_251
                    _var_var_266 = (
                        _var_var_255
                        if _should_apply_bad_weather_ai_guard(_var_var_231)
                        else None
                    )
                    (
                        _var_var_267,
                        _var_var_268,
                        _var_var_269,
                        _var_var_270,
                        _var_var_271,
                    ) = _apply_bad_weather_ai_regime(
                        ai_pred=_var_var_243,
                        physics_pred=_var_var_235,
                        tfs_pred=_var_var_254,
                        regime=_var_var_266,
                    )
                    if _var_var_267 < _var_var_243:
                        _var_var_272 = (
                            _var_var_267 / _var_var_243 if _var_var_243 > 0 else 1.0
                        )
                        _var_var_243 = _var_var_267
                        _var_var_248 *= _var_var_272
                        _var_var_249 *= _var_var_272
                        _var_var_244 = _scale_group_predictions(
                            _var_var_244, _var_var_272
                        )
                        _var_var_250 = _scale_group_predictions(
                            _var_var_250, _var_var_272
                        )
                        _var_var_251 = _scale_group_predictions(
                            _var_var_251, _var_var_272
                        )
                        self._record_forecast_debug("bad_weather_cap")
                    _var_var_267, _var_var_273 = _cap_ai_for_dense_overcast(
                        ai_pred=_var_var_243,
                        physics_pred=_var_var_235,
                        tfs_pred=_var_var_254,
                        clouds=_var_var_210,
                        ghi=_var_var_226,
                        clear_sky=_var_var_214,
                        sun_elevation=_var_var_212,
                        hour=_var_var_25,
                    )
                    if _var_var_267 < _var_var_243:
                        _var_var_272 = (
                            _var_var_267 / _var_var_243 if _var_var_243 > 0 else 1.0
                        )
                        _var_var_243 = _var_var_267
                        _var_var_248 *= _var_var_272
                        _var_var_249 *= _var_var_272
                        _var_var_244 = _scale_group_predictions(
                            _var_var_244, _var_var_272
                        )
                        _var_var_250 = _scale_group_predictions(
                            _var_var_250, _var_var_272
                        )
                        _var_var_251 = _scale_group_predictions(
                            _var_var_251, _var_var_272
                        )
                        self._record_forecast_debug("dense_overcast_ai_cap")
                    if _var_var_252 and _var_var_253:
                        _var_var_274 = (_var_var_248 + _var_var_249) / 2.0
                        if _var_var_274 > 0.005:
                            _var_var_275 = (
                                abs(_var_var_248 - _var_var_249) / _var_var_274
                            )
                        else:
                            _var_var_275 = 0.0
                        if _var_var_275 > DISAGREEMENT_THRESHOLD:
                            _var_var_276 = max(0.5, 1.0 - _var_var_275 * 0.5)
                            _var_var_245 *= _var_var_276
                            _LOGGER.log(
                                5,
                                "Ensemble disagreement %.1f%% > %.0f%%: penalty=%.2f, confidence=%.2f (LSTM=%.3f, Ridge=%.3f)",
                                _var_var_275 * 100,
                                DISAGREEMENT_THRESHOLD * 100,
                                _var_var_276,
                                _var_var_245,
                                _var_var_248,
                                _var_var_249,
                            )
            _var_var_277 = False
            _var_var_278 = _var_var_245
            if _var_var_245 > 0 and _var_var_243 > 0 and (_var_var_235 > 0):
                _var_var_279 = abs(_var_var_243 - _var_var_235) / _var_var_235
                _var_var_280 = _var_var_243 > _var_var_235
                _var_var_281 = _var_var_279
                _var_var_282 = 1.0
                _var_var_283 = 1.0
                _var_var_284 = MAX_AI_PHYSICS_DEVIATION
                if self._physics_calibrator and self._physics_calibrator._factors:
                    _var_var_285 = []
                    for (
                        _var_var_198,
                        _var_var_286,
                    ) in self._physics_calibrator._factors.items():
                        if _var_var_286.sample_count >= 3:
                            _var_var_285.append(_var_var_286.global_factor)
                    if _var_var_285:
                        _var_var_282 = sum(_var_var_285) / len(_var_var_285)
                    if _var_var_280:
                        _var_var_287 = []
                        for _var_var_198 in self._physics_calibrator._factors:
                            _var_var_288 = (
                                self._physics_calibrator.get_correction_factor(
                                    group_name=_var_var_198,
                                    hour=_var_var_25,
                                    cloud_cover=_var_var_210,
                                    sun_elevation=_var_var_212,
                                )
                            )
                            _var_var_287.append(_var_var_288)
                        if _var_var_287:
                            _var_var_283 = sum(_var_var_287) / len(_var_var_287)
                        if _var_var_283 > BUCKET_CAL_THRESHOLD:
                            _var_var_289 = _var_var_283 - 1.0
                            _var_var_281 = max(0.0, _var_var_279 - _var_var_289)
                            _var_var_284 = min(
                                MAX_DYNAMIC_DEVIATION,
                                MAX_AI_PHYSICS_DEVIATION
                                + _var_var_289 * BUCKET_CAL_BOOST,
                            )
                            _LOGGER.log(
                                5,
                                "Asymmetric safeguard [upward]: bucket_cal=%.3f, eff_dev=%.3f, dynamic_max=%.3f (dev=%.3f)",
                                _var_var_283,
                                _var_var_281,
                                _var_var_284,
                                _var_var_279,
                            )
                        else:
                            _var_var_290 = abs(_var_var_282 - 1.0)
                            if _var_var_282 > 1.0 and _var_var_290 > 0.05:
                                _var_var_281 = max(
                                    0.0, _var_var_279 - _var_var_290 * 0.7
                                )
                                _var_var_284 = min(
                                    MAX_DYNAMIC_DEVIATION,
                                    MAX_AI_PHYSICS_DEVIATION
                                    + _var_var_290 * BUCKET_CAL_BOOST,
                                )
                    else:
                        _var_var_290 = abs(_var_var_282 - 1.0)
                        _var_var_291 = _var_var_282 > 1.0
                        if _var_var_280 == _var_var_291 and _var_var_290 > 0.05:
                            _var_var_281 = max(0.0, _var_var_279 - _var_var_290 * 0.7)
                if _var_var_281 <= _var_var_284:
                    _var_var_277 = True
                else:
                    _var_var_292 = False
                    _var_var_293 = False
                    if _var_var_252 and _var_var_253:
                        _var_var_294 = (_var_var_248 + _var_var_249) / 2.0
                        if _var_var_294 > 0.005:
                            _var_var_295 = (
                                abs(_var_var_248 - _var_var_249) / _var_var_294
                            )
                            _var_var_292 = _var_var_295 <= RIDGE_CONFIRMS_THRESHOLD
                        if _var_var_235 > 0.005:
                            _var_var_296 = _var_var_248 / _var_var_235
                            _var_var_297 = _var_var_249 / _var_var_235
                            _var_var_298 = _var_var_296 > 1.1 and _var_var_297 > 1.1
                            _var_var_299 = _var_var_296 < 0.9 and _var_var_297 < 0.9
                            _var_var_293 = _var_var_298 or _var_var_299
                    _var_var_300 = abs(_var_var_282 - 1.0) > 0.15
                    _var_var_301 = await self._check_group_learning_support(
                        _var_var_210, _var_var_25, _var_var_236, _var_var_244
                    )
                    _var_var_302 = _var_var_292 or _var_var_293
                    if _var_var_302 and (_var_var_300 or _var_var_301):
                        _var_var_277 = True
                        _var_var_303 = max(0.0, _var_var_281 - _var_var_284)
                        _var_var_304 = max(0.4, 1.0 - _var_var_303 * 0.4)
                        _var_var_278 = max(_var_var_246, _var_var_245 * _var_var_304)
                        _LOGGER.log(
                            5,
                            "Safeguard graduated [strong]: AI agree + calibrator/learning, dev=%.0f%%, taper=%.2f, conf=%.2f->%.2f",
                            _var_var_279 * 100,
                            _var_var_304,
                            _var_var_245,
                            _var_var_278,
                        )
                    elif _var_var_302:
                        _var_var_277 = True
                        _var_var_303 = max(0.0, _var_var_281 - _var_var_284)
                        _var_var_304 = max(0.2, 1.0 - _var_var_303 * 0.6)
                        _var_var_278 = max(_var_var_246, _var_var_245 * _var_var_304)
                        _LOGGER.log(
                            5,
                            "Safeguard graduated [moderate]: AI agree, dev=%.0f%%, taper=%.2f, conf=%.2f->%.2f",
                            _var_var_279 * 100,
                            _var_var_304,
                            _var_var_245,
                            _var_var_278,
                        )
                    elif _var_var_300:
                        _var_var_277 = True
                        _var_var_305 = abs(_var_var_282 - 1.0)
                        _var_var_306 = min(0.6, 0.25 + _var_var_305 * 0.5)
                        _var_var_278 = max(_var_var_246, _var_var_245 * _var_var_306)
                        _LOGGER.log(
                            5,
                            "Safeguard graduated [calibrator]: cal supports deviation, dev=%.0f%%, conf=%.2f->%.2f",
                            _var_var_279 * 100,
                            _var_var_245,
                            _var_var_278,
                        )
                    elif _var_var_279 <= 1.0:
                        _var_var_277 = True
                        _var_var_278 = max(_var_var_246, _var_var_245 * 0.2)
                        _LOGGER.log(
                            5,
                            "Safeguard graduated [minimal]: dev=%.0f%%, conf=%.2f->%.2f",
                            _var_var_279 * 100,
                            _var_var_245,
                            _var_var_278,
                        )
            _var_var_307 = resolve_morning_ai_underblend_confidence(
                hour=_var_var_25,
                sun_elevation=_var_var_212,
                physics_kwh=_var_var_235,
                ai_kwh=_var_var_243,
                blend_regime=_var_var_231,
                rain=_var_var_227,
                current_confidence=(
                    _var_var_278 if _var_var_277 else float(_var_var_245 or 0.0)
                ),
            )
            if _var_var_307 is not None:
                _var_var_277 = True
                _var_var_278 = float(_var_var_307)
                self._record_forecast_debug("morning_ai_underblend_lift")
            if _var_var_277:
                _var_var_308 = (
                    _var_var_235 * (1 - _var_var_278) + _var_var_243 * _var_var_278
                )
                if _var_var_244 and _var_var_236:
                    _var_var_309 = self._blend_group_details(
                        _var_var_236, _var_var_244, _var_var_278
                    )
                else:
                    _var_var_309 = _var_var_244 or _var_var_236
                _var_var_310 = "ridge" if _var_var_247 else "lstm"
                _var_var_311 = f"physics_{_var_var_310}{int(_var_var_278 * 100)}"
                if _var_var_309:
                    for _var_var_132 in _var_var_309:
                        _var_var_132["source"] = _var_var_311
            else:
                _var_var_308 = _var_var_235
                _var_var_309 = _var_var_236
                if _var_var_309:
                    for _var_var_132 in _var_var_309:
                        _var_var_132["source"] = "physics"
            if _var_var_309 and _var_var_236:
                _var_var_312 = {
                    _var_var_132["name"]: _var_var_132.get(
                        "physics_kwh", _var_var_132.get("power_kwh")
                    )
                    for _var_var_132 in _var_var_236
                }
                _var_var_313 = {
                    _var_var_132["name"]: _var_var_132.get("raw_physics_kwh")
                    for _var_var_132 in _var_var_236
                }
                for _var_var_132 in _var_var_309:
                    if "physics_kwh" not in _var_var_132:
                        _var_var_132["physics_kwh"] = _var_var_312.get(
                            _var_var_132.get("name")
                        )
                    if "raw_physics_kwh" not in _var_var_132:
                        _var_var_132["raw_physics_kwh"] = _var_var_313.get(
                            _var_var_132.get("name")
                        )
            if _var_var_309 and _var_var_244:
                _var_var_314 = {
                    _var_var_132["name"]: _var_var_132["power_kwh"]
                    for _var_var_132 in _var_var_244
                }
                for _var_var_132 in _var_var_309:
                    if "ai_kwh" not in _var_var_132:
                        _var_var_132["ai_kwh"] = (
                            round(_var_var_314[_var_var_132["name"]], 4)
                            if _var_var_132.get("name") in _var_var_314
                            else None
                        )
            if _var_var_309 and _var_var_252 and _var_var_250:
                _var_var_315 = {
                    _var_var_132["name"]: _var_var_132["power_kwh"]
                    for _var_var_132 in _var_var_250
                }
                for _var_var_132 in _var_var_309:
                    if "lstm_kwh" not in _var_var_132:
                        _var_var_132["lstm_kwh"] = (
                            round(_var_var_315[_var_var_132["name"]], 4)
                            if _var_var_132.get("name") in _var_var_315
                            else None
                        )
            if _var_var_309 and _var_var_253 and _var_var_251:
                _var_var_316 = {
                    _var_var_132["name"]: _var_var_132["power_kwh"]
                    for _var_var_132 in _var_var_251
                }
                for _var_var_132 in _var_var_309:
                    if "ridge_kwh" not in _var_var_132:
                        _var_var_132["ridge_kwh"] = (
                            round(_var_var_316[_var_var_132["name"]], 4)
                            if _var_var_132.get("name") in _var_var_316
                            else None
                        )
            _var_var_317 = 0.0
            _var_var_318 = None
            _var_var_319 = None
            if tfs_hour_data:
                _var_var_317 = float(tfs_hour_data.get("p50", 0.0))
                if _var_var_317 > 0:
                    _var_var_320 = tfs_hour_data.get("groups")
                    if _var_var_320 and _var_var_309:
                        _var_var_318 = self._map_tfs_groups(_var_var_320, _var_var_309)
                    _var_var_319 = await _compute_tfs_weight(
                        db=self._db,
                        tfs_hour_data=tfs_hour_data,
                        local_clouds=_var_var_210,
                        group_name=None,
                        hour=hour_dt.hour,
                        adaptive_enabled=True,
                    )
                    _var_var_321 = _var_var_319
                    _var_var_322 = max(
                        _var_var_308,
                        _var_var_235,
                        _var_var_243 if _var_var_243 > 0 else 0.0,
                    )
                    _var_var_319 = _apply_tfs_low_sun_envelope(
                        _var_var_319, _var_var_317, _var_var_322, _var_var_212
                    )
                    if _var_var_319 != _var_var_321:
                        self._record_forecast_debug("tfs_low_sun_envelope")
                    _var_var_319, _var_var_323 = (
                        StaticForecastBlendPolicy.adjust_tfs_weight(
                            _var_var_319,
                            blend_regime=_var_var_231,
                            weather_regime=_var_var_232,
                            regime_learning=_var_var_233,
                            similar_weather=_var_var_234,
                            physics_pred=_var_var_235,
                            ai_pred=_var_var_243,
                            tfs_pred=_var_var_317,
                            rain=_var_var_227,
                            humidity=_var_var_228,
                            sun_elevation=_var_var_212,
                        )
                    )
                    if _var_var_323:
                        self._record_forecast_debug("tfs_policy_adjustment")
                        self._record_forecast_debug(_var_var_323)
                    _var_var_308 = (
                        _var_var_308 * (1 - _var_var_319) + _var_var_317 * _var_var_319
                    )
                    if _var_var_318 and _var_var_309:
                        for _var_var_132 in _var_var_309:
                            _var_var_324 = _var_var_318.get(_var_var_132["name"])
                            if _var_var_324 is not None:
                                _var_var_325 = await _compute_tfs_weight(
                                    db=self._db,
                                    tfs_hour_data=tfs_hour_data,
                                    local_clouds=_var_var_210,
                                    group_name=_var_var_132.get("name"),
                                    hour=hour_dt.hour,
                                    adaptive_enabled=True,
                                )
                                _var_var_326 = max(
                                    float(_var_var_132.get("power_kwh") or 0.0),
                                    float(_var_var_132.get("physics_kwh") or 0.0),
                                    float(_var_var_132.get("ai_kwh") or 0.0),
                                )
                                _var_var_325 = _apply_tfs_low_sun_envelope(
                                    _var_var_325,
                                    _var_var_324,
                                    _var_var_326,
                                    _var_var_212,
                                )
                                _var_var_325, _var_var_38 = (
                                    StaticForecastBlendPolicy.adjust_tfs_weight(
                                        _var_var_325,
                                        blend_regime=_var_var_231,
                                        weather_regime=_var_var_232,
                                        regime_learning=_var_var_233,
                                        similar_weather=_var_var_234,
                                        physics_pred=float(
                                            _var_var_132.get("physics_kwh") or 0.0
                                        ),
                                        ai_pred=float(
                                            _var_var_132.get("ai_kwh") or 0.0
                                        ),
                                        tfs_pred=_var_var_324,
                                        rain=_var_var_227,
                                        humidity=_var_var_228,
                                        sun_elevation=_var_var_212,
                                    )
                                )
                                _var_var_132["power_kwh"] = round(
                                    _var_var_132["power_kwh"] * (1 - _var_var_325)
                                    + _var_var_324 * _var_var_325,
                                    4,
                                )
                                _var_var_132["tfs_weight"] = round(_var_var_325, 3)
                            _var_var_132["tfs_kwh"] = (
                                round(_var_var_324, 4)
                                if _var_var_324 is not None
                                else None
                            )
                            continue
                    self._record_forecast_debug("tfs_adaptive_blend")
            if (not tfs_hour_data or _var_var_317 <= 0) and _var_var_309:
                for _var_var_132 in _var_var_309:
                    _var_var_132["tfs_kwh"] = None
                    _var_var_132["tfs_weight"] = None
            _var_var_327, _var_var_328 = (
                StaticForecastBlendPolicy.adjust_prediction_for_similar_weather(
                    _var_var_308,
                    _var_var_234,
                    physics_pred=_var_var_235,
                    ai_pred=_var_var_243,
                    tfs_pred=_var_var_317,
                    capacity=capacity,
                )
            )
            if _var_var_328:
                if _var_var_309 and _var_var_308 > 0:
                    _var_var_309 = _scale_group_predictions(
                        _var_var_309, _var_var_327 / _var_var_308
                    )
                self._record_forecast_debug("similar_weather_adjustment")
                self._record_forecast_debug(_var_var_328)
                _var_var_308 = _var_var_327
            _var_var_329, _var_var_330 = (
                StaticForecastBlendPolicy.adjust_prediction_for_regime_bias(
                    _var_var_308,
                    _var_var_233,
                    blend_regime=_var_var_231,
                    capacity=capacity,
                )
            )
            if _var_var_330:
                if _var_var_309 and _var_var_308 > 0:
                    _var_var_309 = _scale_group_predictions(
                        _var_var_309, _var_var_329 / _var_var_308
                    )
                self._record_forecast_debug("static_regime_bias")
                self._record_forecast_debug(_var_var_330)
                _var_var_308 = _var_var_329
            _var_var_26 = max(0.0, min(_var_var_308, capacity * 1.0))
            _var_var_309 = normalize_group_predictions_to_total(
                _var_var_309, _var_var_26
            )
            if _var_var_309:
                for _var_var_59 in _var_var_309:
                    _var_var_331 = _var_var_237.get(str(_var_var_59.get("name") or ""))
                    if _var_var_331:
                        _var_var_59["shadow_context"] = _var_var_331
            if should_zero_hourly_yield(
                sun_elevation_deg=_var_var_212,
                hour_start=_var_var_213,
                sunset=_var_var_211.get("sunset"),
            ):
                return zeroed_hour_production_result(sun_elevation_deg=_var_var_212)
            return {
                "production": _var_var_26,
                "groups": _var_var_309,
                "physics_kwh": round(_var_var_235, 4),
                "ai_kwh": (
                    round(_var_var_243, 4)
                    if _var_var_243 > 0 and _var_var_235 > 0
                    else None
                ),
                "ai_confidence": round(_var_var_278, 4) if _var_var_278 > 0 else None,
                "method": "AI + Physics" if _var_var_277 else "physics",
                "ml_contribution": int(_var_var_278 * 100) if _var_var_277 else 0,
                "lstm_kwh": round(_var_var_248, 4) if _var_var_252 else None,
                "ridge_kwh": round(_var_var_249, 4) if _var_var_253 else None,
                "tfs_kwh": round(_var_var_317, 4) if _var_var_317 > 0 else None,
                "tfs_weight": (
                    round(_var_var_319, 3) if _var_var_319 is not None else None
                ),
                "weather_regime": _var_var_232,
                "blend_regime": _var_var_231,
                "shadow_context": _var_var_237,
                "sun_elevation_deg": _var_var_212,
                "ghi_before_cap": _var_var_216,
                "ghi_after_cap": _var_var_217,
                "ghi_cap_applied": _var_var_218,
            }
        except Exception as _var_var_135:
            _LOGGER.warning(
                "Hour calculation failed for %s hour %02d: %s; using physics fallback",
                hour_dt.date().isoformat(),
                hour_dt.hour,
                _var_var_135,
            )
            _LOGGER.debug("Hour calculation traceback", exc_info=True)
            _var_var_211 = self._get_astronomy_for_hour(hour_dt)
            _var_var_212 = _var_var_211.get("sun_elevation_deg", 30)
            _var_var_213 = hour_dt.replace(minute=0, second=0, microsecond=0)
            if should_zero_hourly_yield(
                sun_elevation_deg=_var_var_212,
                hour_start=_var_var_213,
                sunset=_var_var_211.get("sunset"),
            ):
                return zeroed_hour_production_result(sun_elevation_deg=_var_var_212)
            _var_var_26, _var_var_38 = self._calculate_physics_production(
                hour_data, hour_dt, snow_covered_panels
            )
            return {
                "production": _var_var_26,
                "groups": None,
                "physics_kwh": round(_var_var_26, 4),
                "ai_kwh": None,
                "ai_confidence": None,
                "method": "physics",
                "ml_contribution": 0,
                "lstm_kwh": None,
                "ridge_kwh": None,
                "tfs_kwh": None,
                "tfs_weight": None,
                "weather_regime": None,
                "blend_regime": None,
                "sun_elevation_deg": _var_var_212,
                "ghi_before_cap": None,
                "ghi_after_cap": None,
                "ghi_cap_applied": 0,
            }

    def _get_astronomy_for_hour(self, hour_dt: datetime) -> Dict[str, Any]:
        """Get astronomy data for specific hour with enrichment. @zara V16.0.0"""
        try:
            _var_var_178 = get_cache_manager()
            if not _var_var_178 or not _var_var_178.is_loaded():
                return self._default_astronomy(hour_dt.hour)
            _var_var_356 = _var_var_178.get_day_data(hour_dt.date())
            if not _var_var_356:
                return self._default_astronomy(hour_dt.hour)
            _var_var_357 = _var_var_356.get("hourly", {})
            _var_var_358 = str(hour_dt.hour)
            _var_var_359 = _var_var_357.get(_var_var_358, {})
            _var_var_352 = {
                "sun_elevation_deg": _var_var_359.get("elevation_deg", 30),
                "sun_azimuth_deg": _var_var_359.get("azimuth_deg", 180),
                "theoretical_max_kwh": _var_var_359.get("theoretical_max_pv_kwh", 0.5),
                "clear_sky_radiation_wm2": _var_var_359.get(
                    "clear_sky_solar_radiation_wm2", 500
                ),
                "max_elevation_today": _var_var_356.get("max_elevation_deg", 60),
                "sunset": None,
                "hour_start": hour_dt.replace(minute=0, second=0, microsecond=0),
            }
            _var_var_360 = _var_var_356.get(
                "daylight_hours", _var_var_356.get("day_length_hours", 12.0)
            )
            _var_var_352["daylight_hours"] = _var_var_360
            _var_var_361 = _var_var_356.get(
                "sunrise_local", _var_var_356.get("sunrise_time")
            )
            _var_var_362 = _var_var_356.get(
                "sunset_local", _var_var_356.get("sunset_time")
            )
            _var_var_363 = _var_var_356.get(
                "solar_noon_local", _var_var_356.get("solar_noon")
            )
            _var_var_364 = hour_dt.replace(minute=30, second=0, microsecond=0)
            _var_var_25 = hour_dt.hour
            _var_var_365 = max(0.0, min(1.0, (_var_var_25 - 6) / 12.0))
            _var_var_366 = max(0.0, _var_var_25 - 6.0)
            _var_var_367 = max(0.0, 18.0 - _var_var_25)
            _var_var_368 = float(_var_var_25 - 12)
            if _var_var_361 and _var_var_362:
                try:
                    _var_var_369 = self._parse_time_to_datetime(_var_var_361, hour_dt)
                    _var_var_370 = self._parse_time_to_datetime(_var_var_362, hour_dt)
                    _var_var_352["sunset"] = _var_var_370
                    _var_var_371 = (_var_var_370 - _var_var_369).total_seconds()
                    _var_var_372 = (_var_var_364 - _var_var_369).total_seconds()
                    if _var_var_371 > 0:
                        _var_var_365 = max(0.0, min(1.0, _var_var_372 / _var_var_371))
                    _var_var_366 = max(0.0, _var_var_372 / 3600.0)
                    _var_var_367 = max(
                        0.0, (_var_var_370 - _var_var_364).total_seconds() / 3600.0
                    )
                except Exception:
                    pass
            if _var_var_363:
                try:
                    _var_var_373 = self._parse_time_to_datetime(_var_var_363, hour_dt)
                    _var_var_368 = (
                        _var_var_364 - _var_var_373
                    ).total_seconds() / 3600.0
                except Exception:
                    pass
            _var_var_352["day_progress_ratio"] = _var_var_365
            _var_var_352["hours_after_sunrise"] = _var_var_366
            _var_var_352["hours_before_sunset"] = _var_var_367
            _var_var_352["hours_since_solar_noon"] = _var_var_368
            return _var_var_352
        except Exception:
            return self._default_astronomy(hour_dt.hour)

    def _default_astronomy(self, hour: int) -> Dict[str, Any]:
        """Default astronomy values with enrichment. @zara V16.0.0"""
        _var_var_377 = max(0, 45 - abs(hour - 12) * 7)
        _var_var_378 = 90 + (hour - 6) * 15
        _var_var_378 = max(90, min(270, _var_var_378))
        return {
            "sun_elevation_deg": _var_var_377,
            "sun_azimuth_deg": _var_var_378,
            "theoretical_max_kwh": 0.5 if 6 <= hour <= 18 else 0.0,
            "clear_sky_radiation_wm2": max(0, 500 - abs(hour - 12) * 50),
            "max_elevation_today": 60,
            "daylight_hours": 12.0,
            "day_progress_ratio": max(0.0, min(1.0, (hour - 6) / 12.0)),
            "hours_after_sunrise": max(0.0, hour - 6.0),
            "hours_before_sunset": max(0.0, 18.0 - hour),
            "hours_since_solar_noon": float(hour - 12),
        }

    async def _get_recent_daily_up_bias_guard_signal(
        self, *, target_date: str, current_total: float
    ) -> Dict[str, Any]:
        if current_total <= 0.05:
            return {"veto": False, "reason": None}
        _var_var_207 = ("daily_up_bias_rows", target_date)
        if (
            self._forecast_policy_read_cache is not None
            and _var_var_207 in self._forecast_policy_read_cache
        ):
            _var_var_3 = self._forecast_policy_read_cache[_var_var_207]
        else:
            try:
                _var_var_3 = await self._db.fetchall(
                    "SELECT hp.target_date,\n                              SUM(hp.prediction_kwh) AS final_total,\n                              SUM(hp.prediction_kwh_uncapped) AS base_total,\n                              ds.actual_total_kwh\n                       FROM hourly_predictions hp\n                       JOIN daily_summaries ds ON ds.date = hp.target_date\n                       WHERE hp.target_date < ?\n                         AND hp.target_date >= date(?, ?)\n                         AND hp.prediction_kwh_uncapped IS NOT NULL\n                       GROUP BY hp.target_date\n                       HAVING base_total > 0.05\n                          AND ds.actual_total_kwh IS NOT NULL\n                          AND ds.actual_total_kwh > 0\n                       ORDER BY hp.target_date DESC",
                    (
                        target_date,
                        target_date,
                        f"-{DAILY_BIAS_RECENT_GUARD_LOOKBACK_DAYS} day",
                    ),
                )
                if self._forecast_policy_read_cache is not None:
                    self._forecast_policy_read_cache[_var_var_207] = _var_var_3
            except Exception as _var_var_135:
                _LOGGER.debug(
                    "Daily up-bias recent guard unavailable for %s: %s",
                    target_date,
                    _var_var_135,
                )
                return {"veto": False, "reason": None}
        _var_var_432 = []
        for _var_var_208 in _var_var_3:
            try:
                _var_var_433 = max(0.0, float(_var_var_208[1] or 0.0))
                _var_var_434 = max(0.0, float(_var_var_208[2] or 0.0))
                _var_var_421 = max(0.0, float(_var_var_208[3] or 0.0))
            except (TypeError, ValueError):
                continue
            if _var_var_434 <= 0.05 or _var_var_421 <= 0:
                continue
            _var_var_435 = _var_var_434 / current_total
            if (
                not DAILY_BIAS_RECENT_GUARD_MIN_BASE_RATIO
                <= _var_var_435
                <= DAILY_BIAS_RECENT_GUARD_MAX_BASE_RATIO
            ):
                continue
            _var_var_432.append(
                {
                    "final_total": _var_var_433,
                    "base_total": _var_var_434,
                    "actual_total": _var_var_421,
                    "uplift": _var_var_433 / _var_var_434 if _var_var_434 > 0 else 1.0,
                    "base_ratio": (
                        _var_var_421 / _var_var_434 if _var_var_434 > 0 else 1.0
                    ),
                    "final_error": abs(_var_var_433 - _var_var_421),
                    "base_error": abs(_var_var_434 - _var_var_421),
                }
            )
        if len(_var_var_432) < DAILY_BIAS_RECENT_GUARD_MIN_SIMILAR_DAYS:
            return {"veto": False, "reason": None, "similar_days": len(_var_var_432)}
        _var_var_436 = []
        _var_var_437 = []
        _var_var_438 = []
        for _var_var_439 in _var_var_432:
            _var_var_440 = _var_var_439["final_error"] - _var_var_439["base_error"]
            _var_var_441 = max(
                DAILY_BIAS_RECENT_GUARD_MIN_DEGRADATION_KWH,
                _var_var_439["actual_total"]
                * DAILY_BIAS_RECENT_GUARD_DEGRADATION_RATIO,
            )
            if _var_var_439["uplift"] > 1.05 and _var_var_440 > _var_var_441:
                _var_var_436.append(_var_var_439)
            _var_var_442 = max(0.1, _var_var_439["actual_total"] * 0.03)
            if _var_var_439["base_error"] <= _var_var_439["final_error"] + _var_var_442:
                _var_var_437.append(_var_var_439)
            _var_var_443 = max(
                DAILY_BIAS_RECENT_GUARD_SEVERE_DEGRADATION_KWH,
                _var_var_439["actual_total"]
                * DAILY_BIAS_RECENT_GUARD_SEVERE_DEGRADATION_RATIO,
            )
            if _var_var_440 >= _var_var_443:
                _var_var_438.append(_var_var_439)
        _var_var_444 = [_var_var_439["base_ratio"] for _var_var_439 in _var_var_432]
        _var_var_445 = [_var_var_439["uplift"] for _var_var_439 in _var_var_432]
        _var_var_446 = (
            _weighted_quantile(_var_var_444, [1.0] * len(_var_var_444), 0.5) or 1.0
        )
        _var_var_447 = (
            _weighted_quantile(_var_var_445, [1.0] * len(_var_var_445), 0.5) or 1.0
        )
        _var_var_448 = len(_var_var_436) >= 2
        _var_var_449 = len(_var_var_438) >= 1 and len(_var_var_437) >= 2
        if (
            (_var_var_448 or _var_var_449)
            and len(_var_var_437) >= 2
            and (_var_var_446 <= DAILY_BIAS_RECENT_GUARD_MAX_MEDIAN_BASE_RATIO)
            and (_var_var_447 > DAILY_BIAS_RECENT_GUARD_MIN_MEDIAN_UPLIFT)
        ):
            return {
                "veto": True,
                "reason": "similar_recent_harmful_upbias",
                "similar_days": len(_var_var_432),
                "harmful_days": len(_var_var_436),
                "median_base_ratio": round(_var_var_446, 4),
                "median_uplift": round(_var_var_447, 4),
            }
        return {
            "veto": False,
            "reason": None,
            "similar_days": len(_var_var_432),
            "harmful_days": len(_var_var_436),
            "median_base_ratio": round(_var_var_446, 4),
            "median_uplift": round(_var_var_447, 4),
        }

    async def _get_similar_weather_underforecast_signal(
        self,
        *,
        target_date: str,
        weather_regime: str,
        hour: int,
        clouds: float,
        ghi: float,
        dhi: float,
        clear_sky: float,
        rain: float,
        sun_elevation: float,
    ) -> Dict[str, Any]:
        """Return local underforecast evidence from similar final Morning snapshots."""
        if similar_weather_underforecast_is_rain_gated(
            rain=rain, weather_regime=weather_regime
        ):
            self._run_rain_gated[target_date] = (
                str(weather_regime),
                float(rain or 0.0),
            )
            _LOGGER.debug(
                "Similar-weather underforecast signal for %s suppressed by rain gating (regime=%s, rain=%s)",
                target_date,
                weather_regime,
                rain,
            )
            return {
                "underforecast_signal": False,
                "sample_count": 0,
                "weight_sum": 0.0,
                "confidence": 0.0,
                "pred_factor": 1.0,
                "physics_factor": 1.0,
                "max_factor": 1.0,
                "tfs_support_signal": False,
                "tfs_risk_signal": False,
                "pred_signal": False,
                "physics_signal": False,
            }
        if clear_sky > 50 and ghi >= 0:
            _var_var_62 = min(max(ghi / clear_sky, 0.0), 1.05)
        else:
            _var_var_62 = None
        _var_var_64 = None
        if ghi > 5 and dhi >= 0:
            _var_var_64 = min(max(dhi / ghi, 0.0), 1.5)
        _var_var_49 = {
            "weather_regime": weather_regime,
            "hour_bucket": _hour_bucket(hour),
            "season": get_season(int(target_date[5:7])),
            "hour": hour,
            "sun_elevation": sun_elevation,
            "clouds": clouds,
            "rain": rain,
            "ghi_ratio": _var_var_62,
            "diffuse_share": _var_var_64,
        }
        _var_var_414 = await self._get_similar_weather_history(target_date)
        _var_var_424 = []
        for _var_var_425 in _var_var_414:
            _var_var_17 = _similar_weather_weight(
                current=_var_var_49, historic=_var_var_425
            )
            if _var_var_17 > 0.025:
                _var_var_424.append((_var_var_425, _var_var_17))
        if len(_var_var_424) < SIMILAR_WEATHER_MIN_SAMPLES:
            _var_var_452 = {id(_var_var_439[0]) for _var_var_439 in _var_var_424}
            for _var_var_425 in _var_var_414:
                if id(_var_var_425) in _var_var_452:
                    continue
                if (
                    _var_var_425.get("weather_regime") == weather_regime
                    and _var_var_425.get("hour_bucket") == _var_var_49["hour_bucket"]
                    and (_var_var_425.get("season") == _var_var_49["season"])
                ):
                    _var_var_424.append(
                        (
                            _var_var_425,
                            max(
                                0.015,
                                _similar_weather_weight(
                                    current=_var_var_49, historic=_var_var_425
                                ),
                            ),
                        )
                    )
        _var_var_81 = len(_var_var_424)
        _var_var_42 = sum((_var_var_17 for _var_var_38, _var_var_17 in _var_var_424))
        _var_var_352 = {
            "underforecast_signal": False,
            "sample_count": _var_var_81,
            "weight_sum": round(_var_var_42, 4),
            "confidence": 0.0,
            "pred_factor": 1.0,
            "physics_factor": 1.0,
            "max_factor": 1.0,
            "tfs_support_signal": False,
            "tfs_risk_signal": False,
            "pred_signal": False,
            "physics_signal": False,
        }
        if _var_var_81 < SIMILAR_WEATHER_MIN_SAMPLES or _var_var_42 <= 0:
            return _var_var_352
        _var_var_41 = [_var_var_17 for _var_var_38, _var_var_17 in _var_var_424]
        _var_var_453 = [
            float(_var_var_425["pred_factor"])
            for _var_var_425, _var_var_38 in _var_var_424
        ]
        _var_var_454 = [
            float(_var_var_425["physics_factor"])
            for _var_var_425, _var_var_38 in _var_var_424
        ]
        _var_var_455 = _weighted_quantile(_var_var_453, _var_var_41, 0.5) or 1.0
        _var_var_456 = _weighted_quantile(_var_var_453, _var_var_41, 0.25) or 1.0
        _var_var_457 = _weighted_quantile(_var_var_454, _var_var_41, 0.5) or 1.0
        _var_var_458 = _weighted_quantile(_var_var_454, _var_var_41, 0.25) or 1.0
        _var_var_428 = (
            sum(
                (
                    _var_var_17
                    for _var_var_425, _var_var_17 in _var_var_424
                    if float(_var_var_425["pred_factor"]) > 1.1
                )
            )
            / _var_var_42
        )
        _var_var_459 = [
            (_var_var_425, _var_var_17)
            for _var_var_425, _var_var_17 in _var_var_424
            if _var_var_425.get("tfs_factor") is not None
        ]
        if _var_var_459:
            _var_var_460 = [_var_var_17 for _var_var_38, _var_var_17 in _var_var_459]
            _var_var_461 = sum(_var_var_460)
            _var_var_462 = [
                float(_var_var_425["tfs_factor"])
                for _var_var_425, _var_var_38 in _var_var_459
            ]
            _var_var_463 = _weighted_quantile(_var_var_462, _var_var_460, 0.5) or 1.0
            _var_var_464 = _weighted_quantile(_var_var_462, _var_var_460, 0.25) or 1.0
            _var_var_465 = (
                sum(
                    (
                        _var_var_17
                        for _var_var_425, _var_var_17 in _var_var_459
                        if _var_var_425.get("tfs_better_than_blend")
                    )
                )
                / _var_var_461
                if _var_var_461 > 0
                else 0.0
            )
            _var_var_466 = (
                _var_var_461 >= 0.3
                and _var_var_465 >= 0.45
                and (0.65 <= _var_var_463 <= 1.6)
            )
            _var_var_467 = (
                _var_var_461 >= 1.0 and _var_var_465 <= 0.25 and (_var_var_463 < 0.85)
            )
            _var_var_352.update(
                {
                    "tfs_sample_count": len(_var_var_459),
                    "tfs_weight_sum": round(_var_var_461, 4),
                    "tfs_factor_q25": round(_var_var_464, 4),
                    "tfs_factor_median": round(_var_var_463, 4),
                    "tfs_better_share": round(_var_var_465, 4),
                    "tfs_support_signal": _var_var_466,
                    "tfs_risk_signal": _var_var_467,
                }
            )
        _var_var_468 = (
            _var_var_428 >= SIMILAR_WEATHER_MIN_UNDERFORECAST_SHARE
            and _var_var_456 >= SIMILAR_WEATHER_MIN_PRED_FACTOR_Q25
            and (_var_var_455 > 1.1)
        )
        _var_var_469 = (
            _var_var_458 >= SIMILAR_WEATHER_MIN_PHYSICS_FACTOR_Q25
            and _var_var_457 > 1.2
            and (_var_var_455 >= SIMILAR_WEATHER_MIN_PHYSICS_SUPPORT_PRED_MEDIAN)
            and (_var_var_428 >= SIMILAR_WEATHER_MIN_PHYSICS_SUPPORT_SHARE)
        )
        if not _var_var_468 and (not _var_var_469):
            return _var_var_352
        _var_var_73 = min(0.7, max(0.15, _var_var_42 / (_var_var_42 + 2.0)))
        if _var_var_42 < SIMILAR_WEATHER_MAX_WEIGHT_SUM_FOR_THIN_HISTORY:
            _var_var_114 = SIMILAR_WEATHER_MAX_FACTOR_THIN
        elif _var_var_42 < 2.0:
            _var_var_114 = SIMILAR_WEATHER_MAX_FACTOR_MEDIUM
        else:
            _var_var_114 = SIMILAR_WEATHER_MAX_FACTOR_STRONG
        _var_var_109 = 1.0
        if _var_var_468:
            _var_var_109 = min(_var_var_114, 1.0 + (_var_var_455 - 1.0) * _var_var_73)
        _var_var_110 = 1.0
        if _var_var_469:
            _var_var_110 = min(
                SIMILAR_WEATHER_MAX_PHYSICS_FACTOR,
                1.0 + (_var_var_457 - 1.0) * _var_var_73,
            )
        _var_var_352.update(
            {
                "underforecast_signal": True,
                "pred_signal": _var_var_468,
                "physics_signal": _var_var_469,
                "confidence": round(_var_var_73, 4),
                "pred_factor": round(max(1.0, _var_var_109), 4),
                "physics_factor": round(max(1.0, _var_var_110), 4),
                "max_factor": round(_var_var_114, 4),
                "underforecast_share": round(_var_var_428, 4),
                "pred_factor_q25": round(_var_var_456, 4),
                "pred_factor_median": round(_var_var_455, 4),
                "physics_factor_q25": round(_var_var_458, 4),
                "physics_factor_median": round(_var_var_457, 4),
            }
        )
        return _var_var_352
