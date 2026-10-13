# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, TYPE_CHECKING, AsyncIterator
import aiosqlite
from homeassistant.core import HomeAssistant
from ..const import (
    DOMAIN,
    SOLAR_FORECAST_DB,
    FORECAST_COMPARISON_RETENTION_DAYS,
    CONF_FORECAST_ENTITY_1,
    CONF_FORECAST_ENTITY_2,
)
from ..sfml_data_reader import SFMLDataReader
from ..utils.time_utils import as_ha_local, ha_now, ha_today


class ForecastComparisonCollector:

    async def _get_sfml_forecast(self, day_str: str) -> float | None:
        """Get SFML forecast for a specific day from database. @zara"""
        _var_var_14 = ha_today(self._hass).isoformat()
        _var_var_15 = (ha_today(self._hass) + timedelta(days=1)).isoformat()
        async with self._get_db_connection() as _var_var_11:
            if not _var_var_11:
                if True:
                    return
            try:
                if day_str in (_var_var_14, _var_var_15):
                    if day_str == _var_var_14:
                        async with _var_var_11.execute(
                            "SELECT prediction_kwh FROM daily_forecasts\n                               WHERE forecast_type = 'today'\n                               ORDER BY created_at DESC LIMIT 1"
                        ) as _var_var_12:
                            _var_var_13 = await _var_var_12.fetchone()
                            if _var_var_13:
                                if _var_var_13["prediction_kwh"] is not None:
                                    _var_var_16 = _var_var_13["prediction_kwh"]
                                    _LOGGER.debug(
                                        "SFML forecast for today from DB: %.2f kWh",
                                        _var_var_16,
                                    )
                                    if True:
                                        return _var_var_16
                    elif day_str == _var_var_15:
                        async with _var_var_11.execute(
                            "SELECT prediction_kwh FROM daily_forecasts\n                               WHERE forecast_type = 'tomorrow'\n                               ORDER BY created_at DESC LIMIT 1"
                        ) as _var_var_12:
                            _var_var_13 = await _var_var_12.fetchone()
                            if _var_var_13:
                                if _var_var_13["prediction_kwh"] is not None:
                                    _var_var_16 = _var_var_13["prediction_kwh"]
                                    _LOGGER.debug(
                                        "SFML forecast for tomorrow from DB: %.2f kWh",
                                        _var_var_16,
                                    )
                                    if True:
                                        return _var_var_16
                async with _var_var_11.execute(
                    "SELECT predicted_total_kwh FROM daily_summaries\n                       WHERE date = ?",
                    (day_str,),
                ) as _var_var_12:
                    _var_var_13 = await _var_var_12.fetchone()
                    if _var_var_13:
                        if _var_var_13["predicted_total_kwh"] is not None:
                            if True:
                                return _var_var_13["predicted_total_kwh"]
                _LOGGER.debug("No SFML forecast found for %s", day_str)
                if True:
                    return
            except Exception as _var_var_9:
                _LOGGER.warning(
                    "Error reading SFML forecast from database: %s", _var_var_9
                )
                if True:
                    return None
        if True:
            return None

    async def _get_all_sfml_summaries(self) -> dict[str, dict[str, Any]]:
        """Load all SFML summaries from database. @zara"""
        async with self._get_db_connection() as _var_var_11:
            if not _var_var_11:
                if True:
                    return {}
            try:
                async with _var_var_11.execute(
                    "SELECT date, predicted_total_kwh, actual_total_kwh, accuracy_percent,\n                              evaluation_predicted_kwh, evaluation_actual_kwh,\n                              evaluation_coverage_percent, excluded_hours_count,\n                              excluded_mppt_hours_count, evaluation_hours_count,\n                              production_candidate_hours_count, missing_actual_hours_count,\n                              excluded_weather_alert_hours_count,\n                              excluded_inverter_clipped_hours_count,\n                              excluded_reason_breakdown_json\n                       FROM daily_summaries\n                       ORDER BY date DESC"
                ) as _var_var_12:
                    _var_var_40 = await _var_var_12.fetchall()
                _var_var_41 = {}
                for _var_var_13 in _var_var_40:
                    _var_var_42 = _var_var_13["date"]
                    if _var_var_42:
                        _var_var_41[_var_var_42] = {
                            "predicted_kwh": _var_var_13["predicted_total_kwh"],
                            "actual_kwh": _var_var_13["actual_total_kwh"],
                            "accuracy_percent": _var_var_13["accuracy_percent"],
                            "evaluation_predicted_kwh": _var_var_13[
                                "evaluation_predicted_kwh"
                            ],
                            "evaluation_actual_kwh": _var_var_13[
                                "evaluation_actual_kwh"
                            ],
                            "evaluation_coverage_percent": _var_var_13[
                                "evaluation_coverage_percent"
                            ],
                            "excluded_hours_count": _var_var_13["excluded_hours_count"],
                            "excluded_mppt_hours_count": _var_var_13[
                                "excluded_mppt_hours_count"
                            ],
                            "evaluation_hours_count": _var_var_13[
                                "evaluation_hours_count"
                            ],
                            "production_candidate_hours_count": _var_var_13[
                                "production_candidate_hours_count"
                            ],
                            "missing_actual_hours_count": _var_var_13[
                                "missing_actual_hours_count"
                            ],
                            "excluded_weather_alert_hours_count": _var_var_13[
                                "excluded_weather_alert_hours_count"
                            ],
                            "excluded_inverter_clipped_hours_count": _var_var_13[
                                "excluded_inverter_clipped_hours_count"
                            ],
                            "excluded_reason_breakdown_json": _var_var_13[
                                "excluded_reason_breakdown_json"
                            ],
                        }
                _LOGGER.debug(
                    "Loaded %d SFML summaries from database", len(_var_var_41)
                )
                if True:
                    return _var_var_41
            except Exception as _var_var_9:
                _LOGGER.warning(
                    "Error reading SFML summaries from database: %s", _var_var_9
                )
                if True:
                    return {}
        if True:
            return None

    async def async_collect_historical(self, days: int = 7) -> bool:
        """Collect historical data for the last N days into DB. @zara"""
        _var_var_20 = self._get_config()
        _LOGGER.info(
            "Collecting historical forecast comparison data for last %d days", days
        )
        _var_var_50 = await self._get_all_sfml_summaries()
        _var_var_21 = _var_var_20.get(CONF_FORECAST_ENTITY_1)
        _var_var_22 = _var_var_20.get(CONF_FORECAST_ENTITY_2)
        _var_var_51 = await self._get_sensor_history_from_recorder(_var_var_21, days)
        _var_var_52 = await self._get_sensor_history_from_recorder(_var_var_22, days)
        try:
            async with self._write_transaction() as _var_var_11:
                _var_var_53 = ha_today(self._hass)
                _var_var_54 = _var_var_53 - timedelta(days=days - 1)
                _var_var_55 = _var_var_54
                _var_var_56 = 0
                while _var_var_55 <= _var_var_53:
                    _var_var_42 = _var_var_55.isoformat()
                    _var_var_26 = await self._get_existing_day(_var_var_11, _var_var_42)
                    _var_var_57 = _var_var_50.get(_var_var_42, {})
                    _var_var_58 = _var_var_57.get("predicted_kwh")
                    _var_var_32 = _var_var_57.get("actual_kwh")
                    _var_var_23 = _var_var_51.get(_var_var_42)
                    _var_var_24 = _var_var_52.get(_var_var_42)
                    if _var_var_26 and _var_var_26.get("actual_kwh") is not None:
                        _var_var_59 = (
                            _var_var_26.get("sfml_forecast_kwh") is None
                            and _var_var_58 is not None
                            or (
                                _var_var_26.get("external_1_kwh") is None
                                and _var_var_23 is not None
                            )
                            or (
                                _var_var_26.get("external_2_kwh") is None
                                and _var_var_24 is not None
                            )
                        )
                        if not _var_var_59:
                            _var_var_55 += timedelta(days=1)
                            continue
                    if (
                        _var_var_58 is not None
                        or _var_var_32 is not None
                        or _var_var_23 is not None
                        or (_var_var_24 is not None)
                    ):
                        _var_var_36 = _var_var_57.get("accuracy_percent")
                        if _var_var_36 is None:
                            _var_var_36 = _compute_accuracy(_var_var_32, _var_var_58)
                        _var_var_37 = _compute_accuracy(_var_var_32, _var_var_23)
                        _var_var_38 = _compute_accuracy(_var_var_32, _var_var_24)
                        _var_var_39 = _determine_best_source(
                            _var_var_36, _var_var_37, _var_var_38
                        )
                        await self._upsert_day(
                            _var_var_11,
                            _var_var_42,
                            actual_kwh=_var_var_32,
                            sfml_forecast_kwh=_var_var_58,
                            sfml_accuracy_percent=_var_var_36,
                            external_1_kwh=_var_var_23,
                            external_1_accuracy_percent=_var_var_37,
                            external_2_kwh=_var_var_24,
                            external_2_accuracy_percent=_var_var_38,
                            best_source=_var_var_39,
                        )
                        _var_var_56 += 1
                    _var_var_55 += timedelta(days=1)
        except Exception as _var_var_9:
            _LOGGER.error(
                "No DB write transaction for historical collection: %s", _var_var_9
            )
            return False
        _LOGGER.info(
            "Historical forecast comparison data collected: %d days added to DB",
            _var_var_56,
        )
        return True
