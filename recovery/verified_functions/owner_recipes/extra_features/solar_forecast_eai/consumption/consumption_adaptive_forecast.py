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
from ..astronomy_provider import AstronomyProviderAdapter


class AdaptiveForecastEngine:

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
            await self._refresh_weather_data()
            _var_var_61, _var_var_62 = await self._generate_full_forecast(_var_var_1)
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
            _var_var_68 = await self.db.fetchall(
                "SELECT prediction_id, target_hour, actual_kwh\n                   FROM hourly_predictions WHERE target_date = ?",
                (_var_var_1,),
            )
            for _var_var_34 in _var_var_68:
                _var_var_69 = _var_var_34[0]
                _var_var_30 = _var_var_34[1]
                _var_var_26 = _var_var_34[2]
                if _var_var_30 < _var_var_25:
                    if _var_var_26 is not None:
                        await self.db.execute(
                            "UPDATE hourly_predictions\n                               SET prediction_kwh = ?,\n                                   adaptive_corrected = TRUE,\n                                   adaptive_correction_time = ?\n                               WHERE prediction_id = ?",
                            (_var_var_26, _var_var_65, _var_var_69),
                        )
                        await self.db.execute(
                            "UPDATE prediction_panel_groups\n                               SET prediction_kwh = COALESCE(actual_kwh, prediction_kwh)\n                               WHERE prediction_id = ?",
                            (_var_var_69,),
                        )
                        _var_var_66 += 1
                        continue
                    continue
                _var_var_70 = _var_var_63.get(_var_var_30)
                if _var_var_70:
                    _var_var_71 = _var_var_70.get("production_kwh", 0.0)
                    await self.db.execute(
                        "UPDATE hourly_predictions\n                               SET prediction_kwh = ?,\n                                   prediction_kwh_uncapped = ?,\n                                   prediction_method = ?,\n                                   prediction_created_at = ?,\n                                   physics_kwh = ?,\n                                   ai_kwh = ?,\n                                   ai_confidence = ?,\n                                   ml_contribution_percent = ?,\n                                   confidence = ?,\n                                   lstm_kwh = ?,\n                                   ridge_kwh = ?,\n                                   adaptive_corrected = TRUE,\n                                   adaptive_correction_time = ?\n                               WHERE prediction_id = ?",
                        (
                            round(_var_var_71, 4),
                            round(_var_var_71, 4),
                            _var_var_70.get("method", "physics"),
                            _var_var_60,
                            _var_var_70.get("physics_kwh"),
                            _var_var_70.get("ai_kwh"),
                            _var_var_70.get("ai_confidence"),
                            _var_var_70.get("ml_contribution", 0),
                            _var_var_70.get("confidence", 75.0),
                            _var_var_70.get("lstm_kwh"),
                            _var_var_70.get("ridge_kwh"),
                            _var_var_65,
                            _var_var_69,
                        ),
                    )
                    _var_var_72 = _var_var_70.get("groups")
                    if _var_var_72:
                        for _var_var_73 in _var_var_72:
                            await self.db.execute(
                                "UPDATE prediction_panel_groups\n                                       SET prediction_kwh = ?,\n                                           physics_kwh = ?,\n                                           ai_kwh = ?,\n                                           lstm_kwh = ?,\n                                           ridge_kwh = ?\n                                       WHERE prediction_id = ? AND group_name = ?",
                                (
                                    round(_var_var_73.get("power_kwh", 0.0), 4),
                                    (
                                        round(_var_var_73["physics_kwh"], 4)
                                        if _var_var_73.get("physics_kwh")
                                        else None
                                    ),
                                    (
                                        round(_var_var_73["ai_kwh"], 4)
                                        if _var_var_73.get("ai_kwh")
                                        else None
                                    ),
                                    (
                                        round(_var_var_73["lstm_kwh"], 4)
                                        if _var_var_73.get("lstm_kwh")
                                        else None
                                    ),
                                    (
                                        round(_var_var_73["ridge_kwh"], 4)
                                        if _var_var_73.get("ridge_kwh")
                                        else None
                                    ),
                                    _var_var_69,
                                    _var_var_73.get("name"),
                                ),
                            )
                            continue
                    _var_var_74 = _var_var_62.get((_var_var_1, _var_var_30))
                    if _var_var_74:
                        await self.db.execute(
                            "UPDATE prediction_weather\n                                   SET temperature = ?,\n                                       solar_radiation_wm2 = ?,\n                                       wind = ?,\n                                       humidity = ?,\n                                       rain = ?,\n                                       clouds = ?,\n                                       pressure = ?,\n                                       diffuse_radiation = ?,\n                                       direct_radiation = ?\n                                   WHERE prediction_id = ? AND weather_type = 'forecast'",
                            (
                                _var_var_74.get("temperature"),
                                _var_var_74.get("solar_radiation_wm2"),
                                _var_var_74.get("wind"),
                                _var_var_74.get("humidity"),
                                _var_var_74.get("rain"),
                                _var_var_74.get("clouds"),
                                _var_var_74.get("pressure"),
                                _var_var_74.get("diffuse_radiation")
                                or _var_var_74.get("dhi"),
                                _var_var_74.get("direct_radiation"),
                                _var_var_69,
                            ),
                        )
                    _var_var_67 += 1
            _LOGGER.info(
                "ADAPTIVE CORRECTION: %d past hours (pred=actual), %d future hours (new forecast)",
                _var_var_66,
                _var_var_67,
            )
            _var_var_75 = await self._get_daily_forecast_kwh(_var_var_1)
            _var_var_76 = await self.db.fetchone(
                "SELECT SUM(prediction_kwh) FROM hourly_predictions WHERE target_date = ?",
                (_var_var_1,),
            )
            _var_var_77 = (
                round(_var_var_76[0], 2) if _var_var_76 and _var_var_76[0] else 0.0
            )
            await self.db.execute(
                "UPDATE daily_forecasts\n                   SET prediction_kwh = ?,\n                       prediction_kwh_raw = ?,\n                       locked = TRUE,\n                       locked_at = ?,\n                       source = 'adaptive_correction'\n                   WHERE forecast_type = 'today' AND forecast_date = ?",
                (_var_var_77, _var_var_77, _var_var_60, _var_var_1),
            )
            await self.db.execute(
                "INSERT INTO daily_forecast_updates\n                   (forecast_type, forecast_date, prediction_kwh, source, updated_at)\n                   VALUES ('today', ?, ?, 'adaptive_correction', ?)",
                (_var_var_1, _var_var_77, _var_var_60),
            )
            details["original_daily_kwh"] = _var_var_75
            details["corrected_daily_kwh"] = _var_var_77
            _LOGGER.info(
                "ADAPTIVE CORRECTION: daily_forecasts %.2f -> %.2f kWh (re-locked)",
                _var_var_75,
                _var_var_77,
            )
            await self._send_correction_notification(details, _var_var_67)
            if hasattr(self.coordinator, "data_manager"):
                await self.coordinator.data_manager.save_expected_daily_production(
                    _var_var_77
                )
            self.coordinator.expected_daily_production = _var_var_77
            if self.coordinator.data:
                self.coordinator.data["forecast_today"] = _var_var_77
                self.coordinator.data["expected_daily_production"] = _var_var_77
            if hasattr(self.coordinator, "_refresh_hourly_predictions_cache"):
                await self.coordinator._refresh_hourly_predictions_cache()
            self.coordinator.async_update_listeners()
            _LOGGER.info(
                "ADAPTIVE CORRECTION: Complete — %.2f kWh, sensors refreshed",
                _var_var_77,
            )
            return True
        except Exception as _var_var_11:
            _LOGGER.error("ADAPTIVE CORRECTION failed: %s", _var_var_11, exc_info=True)
            return False
