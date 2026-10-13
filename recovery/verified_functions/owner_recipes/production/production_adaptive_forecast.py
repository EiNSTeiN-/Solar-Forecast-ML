# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant
from ..const import (
    ADAPTIVE_DEVIATION_THRESHOLD_PERCENT,
    ADAPTIVE_DEVIATION_MIN_THRESHOLD_KWH,
    ADAPTIVE_DEVIATION_PERCENT_OF_FORECAST,
    ADAPTIVE_MIN_REMAINING_HOURS,
    ADAPTIVE_CLOUD_COVER_DIFF_THRESHOLD,
    DOMAIN,
)
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..data.data_learning_filter import WEATHER_ALERT_UNEXPECTED_SUN
from .production_forecast_regeneration import ForecastRegenerationHelper


class AdaptiveForecastEngine:

    async def _check_special_situations(
        self, predictions: List[Dict[str, Any]], analyzed_hour: Optional[int] = None
    ) -> Optional[str]:
        """
        Check if special situations are active (Snow/Weather-Alert).

        NOTE: Frost (heavy_frost) is NOT a special situation! @zara V16.1
        - Frost = cold air, panels are NOT covered
        - Cold temperatures actually IMPROVE panel efficiency
        - Only physical coverage (snow, ice) affects production

        Snow only blocks if majority of daylight hours (8-16) affected. @zara V16.4.2
        Non-snow alerts block only the hour being scored, not the whole morning.

        Returns:
            String with situation or None if none active. @zara
        """
        _var_var_41 = 0
        _var_var_42 = 0
        _var_var_43 = None
        for _var_var_30 in predictions:
            _var_var_44 = _var_var_30.get("flags", {})
            _var_var_45 = _var_var_44.get("weather_alert_type")
            _var_var_31 = _var_var_30.get("target_hour", 0)
            if 8 <= _var_var_31 <= 16:
                _var_var_42 += 1
                if _var_var_45 == "snow_covered_panels":
                    _var_var_41 += 1
            if analyzed_hour is not None and _var_var_31 != analyzed_hour:
                continue
            if (
                _var_var_45
                and _var_var_45
                not in ("snow_covered_panels", WEATHER_ALERT_UNEXPECTED_SUN)
                and _var_var_44.get("has_weather_alert")
            ):
                if _var_var_43 is None:
                    _var_var_43 = f"Weather alert: {_var_var_45}"
        if _var_var_42 > 0 and _var_var_41 > _var_var_42 * 0.5:
            return "Snow-covered panels"
        return _var_var_43

    async def _execute_correction(self, details: Dict[str, Any]) -> bool:
        """
        Execute midday forecast correction.

        1) Refresh weather API + corrected forecast
        2) Generate new forecast via orchestrator
        3) Past hours (start..now): prediction_kwh = actual_kwh
        4) Future hours (now+1..sunset): prediction_kwh = new forecast
        5) Same for prediction_panel_groups + prediction_weather
        6) daily_forecasts = SUM(prediction_kwh), re-lock
        7) Coordinator state + sensors
        Returns:
            True on success. @zara
        """
        _var_var_0 = details.get("now", dt_util.now())
        _var_var_1 = _var_var_0.date().isoformat()
        _var_var_25 = _var_var_0.hour
        _var_var_60 = _var_var_0
        _LOGGER.info(
            "ADAPTIVE CORRECTION: Start — current hour=%d, date=%s",
            _var_var_25,
            _var_var_1,
        )
        try:
            await self._forecast_regeneration.refresh_weather_data(
                log_prefix="Adaptive"
            )
            _var_var_61, _var_var_62 = (
                await self._forecast_regeneration.generate_full_forecast(
                    _var_var_1, log_prefix="Adaptive"
                )
            )
            if not _var_var_61:
                raise RuntimeError("Full forecast could not be generated")
            _var_var_63 = {}
            for _var_var_64 in _var_var_61:
                if _var_var_64.get("date", "")[:10] == _var_var_1:
                    _var_var_63[_var_var_64.get("hour", -1)] = _var_var_64
            _LOGGER.info(
                "ADAPTIVE CORRECTION: New forecast has %d hours for today",
                len(_var_var_63),
            )
            _var_var_65 = _var_var_60.isoformat()
            _var_var_66 = 0
            _var_var_67 = 0
            _var_var_68 = {}
            async with self.db.transaction():
                _var_var_69 = await self.db.fetchall(
                    "SELECT prediction_id, target_hour, actual_kwh\n                       FROM hourly_predictions WHERE target_date = ?",
                    (_var_var_1,),
                )
                for _var_var_35 in _var_var_69:
                    _var_var_70 = _var_var_35[0]
                    _var_var_31 = _var_var_35[1]
                    _var_var_27 = _var_var_35[2]
                    if _var_var_31 < _var_var_25:
                        if _var_var_27 is not None:
                            await self.db.execute(
                                "UPDATE hourly_predictions\n                                   SET prediction_kwh = ?,\n                                       adaptive_corrected = TRUE,\n                                       adaptive_correction_time = ?\n                                   WHERE prediction_id = ?",
                                (_var_var_27, _var_var_65, _var_var_70),
                                auto_commit=False,
                            )
                            await self.db.execute(
                                "UPDATE prediction_panel_groups\n                                   SET prediction_kwh = COALESCE(actual_kwh, prediction_kwh)\n                                   WHERE prediction_id = ?",
                                (_var_var_70,),
                                auto_commit=False,
                            )
                            _var_var_66 += 1
                            continue
                        continue
                    _var_var_71 = _var_var_63.get(_var_var_31)
                    if _var_var_71:
                        _var_var_72 = _var_var_71.get("production_kwh", 0.0)
                        await self.db.execute(
                            "UPDATE hourly_predictions\n                                   SET prediction_kwh = ?,\n                                       prediction_kwh_uncapped = ?,\n                                       prediction_method = ?,\n                                       prediction_created_at = ?,\n                                       physics_kwh = ?,\n                                       ai_kwh = ?,\n                                       ai_confidence = ?,\n                                       ml_contribution_percent = ?,\n                                       confidence = ?,\n                                       lstm_kwh = ?,\n                                       ridge_kwh = ?,\n                                       tfs_kwh = ?,\n                                       tfs_weight = ?,\n                                       adaptive_corrected = TRUE,\n                                       adaptive_correction_time = ?\n                                   WHERE prediction_id = ?",
                            (
                                round(_var_var_72, 4),
                                round(_var_var_72, 4),
                                _var_var_71.get("method", "physics"),
                                _var_var_60,
                                _var_var_71.get("physics_kwh"),
                                _var_var_71.get("ai_kwh"),
                                _var_var_71.get("ai_confidence"),
                                _var_var_71.get("ml_contribution", 0),
                                _var_var_71.get("confidence", 75.0),
                                _var_var_71.get("lstm_kwh"),
                                _var_var_71.get("ridge_kwh"),
                                _var_var_71.get("tfs_kwh"),
                                _var_var_71.get("tfs_weight"),
                                _var_var_65,
                                _var_var_70,
                            ),
                            auto_commit=False,
                        )
                        _var_var_73 = _var_var_71.get("groups")
                        if _var_var_73:
                            for _var_var_74 in _var_var_73:
                                await self.db.execute(
                                    "UPDATE prediction_panel_groups\n                                           SET prediction_kwh = ?,\n                                               physics_kwh = ?,\n                                               ai_kwh = ?,\n                                               lstm_kwh = ?,\n                                               ridge_kwh = ?,\n                                               tfs_kwh = ?,\n                                               tfs_weight = ?,\n                                               raw_physics_kwh = COALESCE(?, raw_physics_kwh)\n                                           WHERE prediction_id = ? AND group_name = ?",
                                    (
                                        round(_var_var_74.get("power_kwh", 0.0), 4),
                                        (
                                            round(_var_var_74["physics_kwh"], 4)
                                            if _var_var_74.get("physics_kwh")
                                            else None
                                        ),
                                        (
                                            round(_var_var_74["ai_kwh"], 4)
                                            if _var_var_74.get("ai_kwh")
                                            else None
                                        ),
                                        (
                                            round(_var_var_74["lstm_kwh"], 4)
                                            if _var_var_74.get("lstm_kwh")
                                            else None
                                        ),
                                        (
                                            round(_var_var_74["ridge_kwh"], 4)
                                            if _var_var_74.get("ridge_kwh")
                                            else None
                                        ),
                                        (
                                            round(_var_var_74["tfs_kwh"], 4)
                                            if _var_var_74.get("tfs_kwh")
                                            else None
                                        ),
                                        (
                                            round(_var_var_74["tfs_weight"], 3)
                                            if _var_var_74.get("tfs_weight") is not None
                                            else None
                                        ),
                                        (
                                            round(_var_var_74["raw_physics_kwh"], 4)
                                            if _var_var_74.get("raw_physics_kwh")
                                            is not None
                                            else None
                                        ),
                                        _var_var_70,
                                        _var_var_74.get("name"),
                                    ),
                                    auto_commit=False,
                                )
                                continue
                        _var_var_75 = _var_var_62.get((_var_var_1, _var_var_31))
                        if _var_var_75:
                            await self.db.execute(
                                "UPDATE prediction_weather\n                                       SET temperature = ?,\n                                           solar_radiation_wm2 = ?,\n                                           wind = ?,\n                                           humidity = ?,\n                                           rain = ?,\n                                           clouds = ?,\n                                           pressure = ?,\n                                           diffuse_radiation = ?,\n                                           direct_radiation = ?\n                                       WHERE prediction_id = ? AND weather_type = 'forecast'",
                                (
                                    _var_var_75.get("temperature"),
                                    _var_var_75.get("solar_radiation_wm2"),
                                    _var_var_75.get("wind"),
                                    _var_var_75.get("humidity"),
                                    _var_var_75.get("rain"),
                                    _var_var_75.get("clouds"),
                                    _var_var_75.get("pressure"),
                                    _var_var_75.get("diffuse_radiation")
                                    or _var_var_75.get("dhi"),
                                    _var_var_75.get("direct_radiation"),
                                    _var_var_70,
                                ),
                                auto_commit=False,
                            )
                        _var_var_67 += 1
                _var_var_76 = await self._get_daily_forecast_kwh(_var_var_1)
                _var_var_77 = await self.db.fetchone(
                    "SELECT SUM(prediction_kwh) FROM hourly_predictions WHERE target_date = ?",
                    (_var_var_1,),
                )
                _var_var_78 = (
                    round(_var_var_77[0], 2) if _var_var_77 and _var_var_77[0] else 0.0
                )
                await self.db.execute(
                    "UPDATE daily_forecasts\n                       SET prediction_kwh = ?,\n                           prediction_kwh_raw = ?,\n                           locked = TRUE,\n                           locked_at = ?,\n                           source = 'adaptive_correction'\n                       WHERE forecast_type = 'today' AND forecast_date = ?",
                    (_var_var_78, _var_var_78, _var_var_60, _var_var_1),
                    auto_commit=False,
                )
                await self.db.execute(
                    "INSERT INTO daily_forecast_updates\n                       (forecast_type, forecast_date, prediction_kwh, source, updated_at)\n                       VALUES ('today', ?, ?, 'adaptive_correction', ?)",
                    (_var_var_1, _var_var_78, _var_var_60),
                    auto_commit=False,
                )
                details["original_daily_kwh"] = _var_var_76
                details["corrected_daily_kwh"] = _var_var_78
                _var_var_79 = getattr(self.coordinator, "data_manager", None)
                _var_var_80 = getattr(_var_var_79, "operational_forecast_handler", None)
                if _var_var_80 is None:
                    raise RuntimeError(
                        "Operational forecast handler is not available for adaptive correction"
                    )
                _var_var_68 = await _var_var_80.publish_adaptive_correction_snapshot(
                    snapshot_date=_var_var_1,
                    corrected_daily_kwh=_var_var_78,
                    cutoff_hour=_var_var_25,
                    created_at=_var_var_60,
                )
            _LOGGER.info(
                "ADAPTIVE CORRECTION: %d past hours (pred=actual), %d future hours (new forecast)",
                _var_var_66,
                _var_var_67,
            )
            _LOGGER.info(
                "ADAPTIVE CORRECTION: daily_forecasts %.2f -> %.2f kWh (re-locked)",
                _var_var_76,
                _var_var_78,
            )
            _LOGGER.info(
                "ADAPTIVE CORRECTION: OPS snapshot #%s synchronized (%d hourly, %d weather, %d groups)",
                _var_var_68.get("snapshot_sequence"),
                _var_var_68.get("hourly_rows", 0),
                _var_var_68.get("weather_rows", 0),
                _var_var_68.get("group_rows", 0),
            )
            await self._send_correction_notification(details, _var_var_67)
            if hasattr(self.coordinator, "data_manager"):
                await self.coordinator.data_manager.save_expected_daily_production(
                    _var_var_78
                )
            self.coordinator.expected_daily_production = _var_var_78
            if self.coordinator.data:
                self.coordinator.data["forecast_today"] = _var_var_78
                self.coordinator.data["expected_daily_production"] = _var_var_78
            if hasattr(self.coordinator, "_refresh_hourly_predictions_cache"):
                await self.coordinator._refresh_hourly_predictions_cache()
            self.coordinator.async_update_listeners()
            _LOGGER.info(
                "ADAPTIVE CORRECTION: Complete — %.2f kWh, sensors refreshed",
                _var_var_78,
            )
            return True
        except Exception as _var_var_11:
            _LOGGER.error("ADAPTIVE CORRECTION failed: %s", _var_var_11, exc_info=True)
            return False
