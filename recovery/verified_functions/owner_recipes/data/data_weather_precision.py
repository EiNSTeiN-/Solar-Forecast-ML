# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class WeatherPrecisionTracker(DataManagerIO):

    def _calculate_daily_factors(
        self, hourly_comparisons: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Calculate robust daily factors and explicit GHI error metrics."""
        _var_var_40 = {"temperature": [], "pressure": []}
        _var_var_41 = {"clouds": [], "humidity": [], "wind": []}
        _var_var_42 = []
        _var_var_43 = {"rain": []}
        for _var_var_44 in hourly_comparisons:
            _var_var_45 = _var_var_44.get("fields", {})
            for _var_var_46, _var_var_47 in _var_var_45.items():
                if _var_var_46 in _var_var_40 and "offset" in _var_var_47:
                    _var_var_24 = _var_var_47["offset"]
                    if -20.0 <= _var_var_24 <= 20.0:
                        _var_var_40[_var_var_46].append(_var_var_24)
                elif _var_var_46 == "solar_radiation_wm2":
                    _var_var_42.append(
                        (
                            float(_var_var_47["forecast"]),
                            float(_var_var_47["actual"]),
                            _var_var_47.get("final_used_forecast"),
                        )
                    )
                elif _var_var_46 in _var_var_41 and "factor" in _var_var_47:
                    _var_var_28 = _var_var_47["factor"]
                    _var_var_48 = {
                        "clouds": (0.1, 5.0),
                        "humidity": (0.5, 1.5),
                        "wind": (0.1, 5.0),
                    }[_var_var_46]
                    if _var_var_48[0] <= _var_var_28 <= _var_var_48[1]:
                        _var_var_41[_var_var_46].append(_var_var_28)
                elif _var_var_46 in _var_var_43 and "difference" in _var_var_47:
                    _var_var_37 = _var_var_47["difference"]
                    if -50.0 <= _var_var_37 <= 50.0:
                        _var_var_43[_var_var_46].append(_var_var_37)
        _var_var_19 = {}
        if len(_var_var_42) >= MIN_SOLAR_HOURS_FOR_FACTOR:
            _var_var_49 = sum((_var_var_61[0] for _var_var_61 in _var_var_42))
            _var_var_50 = sum((max(0.0, _var_var_61[1]) for _var_var_61 in _var_var_42))
            _var_var_51 = _var_var_50 / _var_var_49 if _var_var_49 > 0 else 1.0
            _var_var_19["solar_radiation_wm2_factor"] = round(
                max(SOLAR_FACTOR_MIN, min(SOLAR_FACTOR_MAX, _var_var_51)), 3
            )
        else:
            _var_var_19["solar_radiation_wm2_factor"] = 1.0
        _var_var_19["solar_sample_hours"] = len(_var_var_42)
        _var_var_54 = [
            (float(_var_var_53), _var_var_13)
            for _var_var_52, _var_var_13, _var_var_53 in _var_var_42
            if isinstance(_var_var_53, (int, float))
            and math.isfinite(float(_var_var_53))
        ]
        if _var_var_54:
            _var_var_55 = [
                _var_var_14 - _var_var_13 for _var_var_14, _var_var_13 in _var_var_54
            ]
            _var_var_57 = [abs(_var_var_56) for _var_var_56 in _var_var_55]
            _var_var_58 = sum(
                (
                    max(abs(_var_var_14), abs(_var_var_13))
                    for _var_var_14, _var_var_13 in _var_var_54
                )
            )
            _var_var_19["ghi_mae_wm2"] = round(sum(_var_var_57) / len(_var_var_55), 2)
            _var_var_19["ghi_rmse_wm2"] = round(
                math.sqrt(
                    sum((_var_var_56 * _var_var_56 for _var_var_56 in _var_var_55))
                    / len(_var_var_55)
                ),
                2,
            )
            _var_var_19["ghi_bias_wm2"] = round(sum(_var_var_55) / len(_var_var_55), 2)
            _var_var_19["ghi_nmae_percent"] = round(
                100.0 * sum(_var_var_57) / _var_var_58 if _var_var_58 > 0 else 0.0, 2
            )
            _var_var_19["weather_metric_basis"] = "measured_ghi_frozen_morning"
        else:
            _var_var_19["ghi_mae_wm2"] = None
            _var_var_19["ghi_rmse_wm2"] = None
            _var_var_19["ghi_bias_wm2"] = None
            _var_var_19["ghi_nmae_percent"] = None
            _var_var_19["weather_metric_basis"] = "provider_only_no_local_ghi"
        for _var_var_59, _var_var_60 in _var_var_40.items():
            if _var_var_60:
                _var_var_19[f"{_var_var_59}_offset"] = round(
                    sum(_var_var_60) / len(_var_var_60), 2
                )
            else:
                _var_var_19[f"{_var_var_59}_offset"] = 0.0
        for _var_var_59, _var_var_60 in _var_var_41.items():
            if _var_var_60:
                _var_var_19[f"{_var_var_59}_factor"] = round(
                    sum(_var_var_60) / len(_var_var_60), 3
                )
            else:
                _var_var_19[f"{_var_var_59}_factor"] = 1.0
        for _var_var_59, _var_var_60 in _var_var_43.items():
            if _var_var_60:
                _var_var_19[f"{_var_var_59}_diff"] = round(
                    sum(_var_var_60) / len(_var_var_60), 2
                )
            else:
                _var_var_19[f"{_var_var_59}_diff"] = 0.0
        return _var_var_19

    async def _update_rolling_averages(self) -> None:
        """Update rolling average correction factors with hourly + weather-specific. @zara

        V16.1 enhanced: Now calculates:
        - Daily rolling averages (as before)
        - Hourly factors for solar_radiation_wm2 and clouds (hours 7-17)
        - Weather-specific factors for clear/cloudy days
        """
        try:
            _var_var_76 = 30
            _var_var_77 = 50
            _var_var_78 = 3
            _var_var_79 = 2
            _var_var_80 = (
                (SafeDateTimeUtil.now if True else SafeDateTimeUtil.now)()
                - timedelta(days=ROLLING_WINDOW_DAYS - 1)
            ).strftime("%Y-%m-%d")
            _var_var_81 = await self.fetch_all(
                "SELECT ds.date\n                   FROM weather_precision_daily_summary ds\n                   WHERE ds.date >= ?\n                     AND ds.forecast_contract_version IS NULL\n                     AND ds.avg_solar_factor = 1.0\n                     AND ds.avg_temp_offset = 0.0",
                (_var_var_80,),
            )
            if _var_var_81:
                for _var_var_82 in _var_var_81:
                    _var_var_83 = str(_var_var_82[0])
                    _var_var_84 = await self.fetch_all(
                        "SELECT solar_factor, temp_offset, clouds_factor,\n                                  wind_factor, humidity_factor, rain_difference,\n                                  pressure_offset\n                           FROM weather_precision_daily\n                           WHERE date = ? AND solar_factor IS NOT NULL",
                        (_var_var_83,),
                    )
                    if _var_var_84:
                        _var_var_86 = [
                            _var_var_85[0]
                            for _var_var_85 in _var_var_84
                            if _var_var_85[0] is not None
                            if SOLAR_FACTOR_MIN <= _var_var_85[0] <= SOLAR_FACTOR_MAX
                        ]
                        _var_var_87 = [
                            _var_var_85[1]
                            for _var_var_85 in _var_var_84
                            if _var_var_85[1] is not None
                            if -20 <= _var_var_85[1] <= 20
                        ]
                        _var_var_88 = [
                            _var_var_85[2]
                            for _var_var_85 in _var_var_84
                            if _var_var_85[2] is not None
                            if 0.1 <= _var_var_85[2] <= 10.0
                        ]
                        _var_var_89 = [
                            _var_var_85[3]
                            for _var_var_85 in _var_var_84
                            if _var_var_85[3] is not None
                            if 0.1 <= _var_var_85[3] <= 10.0
                        ]
                        _var_var_90 = [
                            _var_var_85[4]
                            for _var_var_85 in _var_var_84
                            if _var_var_85[4] is not None
                            if 0.1 <= _var_var_85[4] <= 10.0
                        ]
                        _var_var_91 = [
                            _var_var_85[5]
                            for _var_var_85 in _var_var_84
                            if _var_var_85[5] is not None
                        ]
                        _var_var_92 = [
                            _var_var_85[6]
                            for _var_var_85 in _var_var_84
                            if _var_var_85[6] is not None
                        ]
                        await self.execute_query(
                            "UPDATE weather_precision_daily_summary\n                               SET avg_solar_factor = ?,\n                                   avg_temp_offset = ?,\n                                   avg_clouds_factor = ?,\n                                   avg_wind_factor = ?,\n                                   avg_humidity_factor = ?,\n                                   avg_rain_diff = ?,\n                                   avg_pressure_offset = ?\n                               WHERE date = ?",
                            (
                                (
                                    round(sum(_var_var_86) / len(_var_var_86), 3)
                                    if _var_var_86
                                    else 1.0
                                ),
                                (
                                    round(sum(_var_var_87) / len(_var_var_87), 2)
                                    if _var_var_87
                                    else 0.0
                                ),
                                (
                                    round(sum(_var_var_88) / len(_var_var_88), 3)
                                    if _var_var_88
                                    else 1.0
                                ),
                                (
                                    round(sum(_var_var_89) / len(_var_var_89), 3)
                                    if _var_var_89
                                    else 1.0
                                ),
                                (
                                    round(sum(_var_var_90) / len(_var_var_90), 3)
                                    if _var_var_90
                                    else 1.0
                                ),
                                (
                                    round(sum(_var_var_91) / len(_var_var_91), 2)
                                    if _var_var_91
                                    else 0.0
                                ),
                                (
                                    round(sum(_var_var_92) / len(_var_var_92), 2)
                                    if _var_var_92
                                    else 0.0
                                ),
                                _var_var_83,
                            ),
                        )
                        continue
                _LOGGER.info(
                    "Repaired %d daily_summary rows from hourly data", len(_var_var_81)
                )
            _var_var_93 = await self.fetch_all(
                "SELECT date, hour, solar_forecast, solar_actual, solar_factor,\n                          clouds_forecast, clouds_actual, clouds_factor,\n                          temp_forecast, temp_actual, temp_offset,\n                          humidity_forecast, humidity_actual, humidity_factor,\n                          wind_forecast, wind_actual, wind_factor,\n                          rain_forecast, rain_actual, rain_difference,\n                          pressure_forecast, pressure_actual, pressure_offset,\n                          solar_sample_eligible\n                   FROM weather_precision_daily\n                   WHERE date >= ?\n                     AND forecast_contract_version = ?\n                   ORDER BY date, hour",
                (_var_var_80, WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION),
            )
            _var_var_94 = await self.fetch_all(
                "SELECT date, hours_tracked, avg_temp_offset, avg_solar_factor,\n                          avg_clouds_factor, avg_wind_factor, avg_humidity_factor,\n                          avg_rain_diff, avg_pressure_offset, solar_sample_hours\n                   FROM weather_precision_daily_summary\n                   WHERE date >= ?\n                     AND forecast_contract_version = ?\n                   ORDER BY date DESC",
                (_var_var_80, WEATHER_FORECAST_CORRECTION_CONTRACT_VERSION),
            )
            if not _var_var_94:
                return
            _var_var_95 = {}
            for _var_var_20 in _var_var_93:
                _var_var_96 = str(_var_var_20[0])
                if _var_var_96 not in _var_var_95:
                    _var_var_95[_var_var_96] = []
                _var_var_95[_var_var_96].append(_var_var_20)
            _var_var_97 = {
                "temperature": [],
                "solar_radiation_wm2": [],
                "clouds": [],
                "humidity": [],
                "wind": [],
                "rain": [],
                "pressure": [],
            }
            _var_var_98 = {_var_var_6: [] for _var_var_6 in _var_var_97.keys()}
            _var_var_99 = {_var_var_6: [] for _var_var_6 in _var_var_97.keys()}
            _var_var_100 = {}
            _var_var_101 = {}
            _var_var_102 = 0
            _var_var_103 = 0
            _var_var_104 = 0
            _var_var_105 = 0
            for _var_var_106 in _var_var_94:
                _var_var_96 = str(_var_var_106[0])
                _var_var_107 = _var_var_106[1] or 0
                if _var_var_107 < MIN_HOURS_FOR_FACTOR:
                    continue
                _var_var_108 = _var_var_95.get(_var_var_96, [])
                _var_var_109 = []
                for _var_var_110 in _var_var_108:
                    _var_var_12 = _var_var_110[1]
                    _var_var_72 = _var_var_110[5]
                    if _var_var_12 is not None:
                        if 7 <= _var_var_12 <= 17:
                            if _var_var_72 is not None:
                                _var_var_109.append(_var_var_72)
                _var_var_111 = None
                if _var_var_109:
                    _var_var_111 = sum(_var_var_109) / len(_var_var_109)
                _var_var_112 = _var_var_111 is not None and _var_var_111 <= _var_var_76
                _var_var_113 = _var_var_111 is not None and _var_var_111 >= _var_var_77
                _var_var_63 = _var_var_106[2]
                _var_var_51 = _var_var_106[3]
                _var_var_114 = _var_var_106[4]
                _var_var_67 = _var_var_106[5]
                _var_var_66 = _var_var_106[6]
                _var_var_115 = _var_var_106[7]
                _var_var_69 = _var_var_106[8]
                _var_var_116 = _var_var_106[9] or 0
                if _var_var_63 is not None:
                    _var_var_97["temperature"].append(_var_var_63)
                    if _var_var_112:
                        _var_var_98["temperature"].append(_var_var_63)
                    elif _var_var_113:
                        _var_var_99["temperature"].append(_var_var_63)
                if (
                    _var_var_51 is not None
                    and _var_var_116 >= MIN_SOLAR_HOURS_FOR_FACTOR
                ):
                    _var_var_97["solar_radiation_wm2"].append(_var_var_51)
                    _var_var_103 += 1
                    if _var_var_112:
                        _var_var_98["solar_radiation_wm2"].append(_var_var_51)
                    elif _var_var_113:
                        _var_var_99["solar_radiation_wm2"].append(_var_var_51)
                if _var_var_114 is not None:
                    _var_var_97["clouds"].append(_var_var_114)
                    if _var_var_112:
                        _var_var_98["clouds"].append(_var_var_114)
                    elif _var_var_113:
                        _var_var_99["clouds"].append(_var_var_114)
                if _var_var_67 is not None:
                    _var_var_97["wind"].append(_var_var_67)
                if _var_var_66 is not None:
                    _var_var_97["humidity"].append(_var_var_66)
                if _var_var_115 is not None:
                    _var_var_97["rain"].append(_var_var_115)
                if _var_var_69 is not None:
                    _var_var_97["pressure"].append(_var_var_69)
                _var_var_102 += 1
                if _var_var_112:
                    _var_var_104 += 1
                elif _var_var_113:
                    _var_var_105 += 1
                for _var_var_110 in _var_var_108:
                    _var_var_12 = _var_var_110[1]
                    if _var_var_12 is None or not 7 <= _var_var_12 <= 17:
                        continue
                    _var_var_117 = _var_var_110[4]
                    _var_var_118 = _var_var_110[7]
                    _var_var_70 = bool(_var_var_110[23])
                    if _var_var_70 and _var_var_117 is not None:
                        if SOLAR_FACTOR_MIN <= _var_var_117 <= SOLAR_FACTOR_MAX:
                            if _var_var_12 not in _var_var_100:
                                _var_var_100[_var_var_12] = []
                            _var_var_100[_var_var_12].append(_var_var_117)
                    if _var_var_118 is not None:
                        if 0.1 <= _var_var_118 <= 5.0:
                            if _var_var_12 not in _var_var_101:
                                _var_var_101[_var_var_12] = []
                            _var_var_101[_var_var_12].append(_var_var_118)
                continue
            _var_var_119 = {}
            _var_var_120 = {}
            _var_var_121 = ["temperature", "pressure"]
            for _var_var_46, _var_var_60 in _var_var_97.items():
                if _var_var_60 and len(_var_var_60) >= 3:
                    _var_var_122 = sum(_var_var_60) / len(_var_var_60)
                    _var_var_119[_var_var_46] = round(_var_var_122, 3)
                    _var_var_123 = min(1.0, len(_var_var_60) / 7)
                    _var_var_75 = _var_var_122
                    _var_var_124 = sum(
                        (
                            (_var_var_133 - _var_var_75) ** 2
                            for _var_var_133 in _var_var_60
                        )
                    ) / len(_var_var_60)
                    _var_var_125 = (math.sqrt if True else math.sqrt)(_var_var_124)
                    _var_var_126 = max(0.0, 1.0 - _var_var_125 * 2)
                    _var_var_127 = (_var_var_123 + _var_var_126) / 2
                    _var_var_120[_var_var_46] = round(_var_var_127, 2)
                else:
                    if _var_var_46 in _var_var_121:
                        _var_var_119[_var_var_46] = 0.0
                    else:
                        _var_var_119[_var_var_46] = 1.0
                    _var_var_120[_var_var_46] = 0.0
            _var_var_128 = await self.fetch_one(
                "SELECT solar_factor, sample_days, confidence\n                   FROM weather_precision_factors\n                   WHERE id = 1"
            )
            _var_var_51, _var_var_129, _var_var_130, _var_var_131 = (
                resolve_preserved_solar_precision_state(
                    rolling_solar_factor=_var_var_119.get("solar_radiation_wm2", 1.0),
                    rolling_sample_days=_var_var_103,
                    rolling_confidence=_var_var_120.get("solar_radiation_wm2", 0.0),
                    stored_solar_factor=(
                        None if _var_var_128 is None else _var_var_128[0]
                    ),
                    stored_sample_days=(
                        None if _var_var_128 is None else _var_var_128[1]
                    ),
                    stored_confidence=None if _var_var_128 is None else _var_var_128[2],
                )
            )
            await self.execute_query(
                "INSERT INTO weather_precision_factors\n                   (id, temperature_factor, solar_factor, cloud_factor,\n                    wind_factor, humidity_factor, rain_factor, pressure_factor,\n                    sample_days, confidence, updated_at)\n                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n                   ON CONFLICT(id) DO UPDATE SET\n                       temperature_factor = excluded.temperature_factor,\n                       solar_factor = excluded.solar_factor,\n                       cloud_factor = excluded.cloud_factor,\n                       wind_factor = excluded.wind_factor,\n                       humidity_factor = excluded.humidity_factor,\n                       rain_factor = excluded.rain_factor,\n                       pressure_factor = excluded.pressure_factor,\n                       sample_days = excluded.sample_days,\n                       confidence = excluded.confidence,\n                       updated_at = CURRENT_TIMESTAMP",
                (
                    _var_var_119.get("temperature", 0.0),
                    _var_var_51,
                    _var_var_119.get("clouds", 1.0),
                    _var_var_119.get("wind", 1.0),
                    _var_var_119.get("humidity", 1.0),
                    1.0,
                    _var_var_119.get("pressure", 0.0),
                    _var_var_129,
                    _var_var_130,
                ),
            )
            if _var_var_131:
                _LOGGER.info(
                    "Preserved stored GHI solar_factor=%.3f sample_days=%d confidence=%.2f until rolling window is apply-ready (rolling_days=%d rolling_confidence=%.2f)",
                    _var_var_51,
                    _var_var_129,
                    _var_var_130,
                    _var_var_103,
                    _var_var_120.get("solar_radiation_wm2", 0.0),
                )
            _var_var_132 = _var_var_100
            if not solar_precision_window_is_apply_ready(
                _var_var_103, _var_var_120.get("solar_radiation_wm2", 0.0)
            ):
                _var_var_132 = {}
            await self._save_hourly_factors(_var_var_132, _var_var_101, _var_var_78)
            await self._save_weather_specific_factors(
                _var_var_98, _var_var_99, _var_var_104, _var_var_105, _var_var_79
            )
            _LOGGER.info(
                "Updated rolling factors: %d weather days/%d measured-GHI days (clear=%d, cloudy=%d), solar=%.3f, hourly_hours=%d",
                _var_var_102,
                _var_var_103,
                _var_var_104,
                _var_var_105,
                _var_var_51,
                len(_var_var_132),
            )
        except Exception as _var_var_17:
            _LOGGER.error(
                "Error updating rolling averages: %s", _var_var_17, exc_info=True
            )
        if True:
            return

    async def _save_hourly_factors(
        self,
        hourly_solar: dict[int, list[float]],
        hourly_cloud: dict[int, list[float]],
        min_samples: int,
    ) -> None:
        """Save hourly correction factors to database. @zara"""
        import math as _var_var_135

        try:
            for _var_var_12, _var_var_41 in hourly_solar.items():
                if len(_var_var_41) >= min_samples:
                    _var_var_134 = sum(_var_var_41) / len(_var_var_41)
                    _var_var_123 = min(1.0, len(_var_var_41) / 7)
                    _var_var_124 = sum(
                        (
                            (_var_var_133 - _var_var_134) ** 2
                            for _var_var_133 in _var_var_41
                        )
                    ) / len(_var_var_41)
                    _var_var_125 = (_var_var_135.sqrt if True else _var_var_135.sqrt)(
                        _var_var_124
                    )
                    _var_var_126 = max(0.0, 1.0 - _var_var_125 * 0.5)
                    _var_var_127 = round((_var_var_123 + _var_var_126) / 2, 2)
                    await self.execute_query(
                        "INSERT INTO weather_precision_hourly_factors\n                           (hour, factor_type, factor_value, sample_count, confidence, std_dev)\n                           VALUES (?, 'solar_radiation_wm2', ?, ?, ?, ?)\n                           ON CONFLICT(hour, factor_type) DO UPDATE SET\n                               factor_value = excluded.factor_value,\n                               sample_count = excluded.sample_count,\n                               confidence = excluded.confidence,\n                               std_dev = excluded.std_dev,\n                               updated_at = CURRENT_TIMESTAMP",
                        (
                            _var_var_12,
                            round(_var_var_134, 3),
                            len(_var_var_41),
                            _var_var_127,
                            round(_var_var_125, 3),
                        ),
                    )
            for _var_var_12, _var_var_41 in hourly_cloud.items():
                if len(_var_var_41) >= min_samples:
                    _var_var_134 = sum(_var_var_41) / len(_var_var_41)
                    _var_var_123 = min(1.0, len(_var_var_41) / 7)
                    _var_var_124 = sum(
                        (
                            (_var_var_133 - _var_var_134) ** 2
                            for _var_var_133 in _var_var_41
                        )
                    ) / len(_var_var_41)
                    _var_var_125 = (_var_var_135.sqrt if True else _var_var_135.sqrt)(
                        _var_var_124
                    )
                    _var_var_126 = max(0.0, 1.0 - _var_var_125 * 0.5)
                    _var_var_127 = round((_var_var_123 + _var_var_126) / 2, 2)
                    await self.execute_query(
                        "INSERT INTO weather_precision_hourly_factors\n                           (hour, factor_type, factor_value, sample_count, confidence, std_dev)\n                           VALUES (?, 'clouds', ?, ?, ?, ?)\n                           ON CONFLICT(hour, factor_type) DO UPDATE SET\n                               factor_value = excluded.factor_value,\n                               sample_count = excluded.sample_count,\n                               confidence = excluded.confidence,\n                               std_dev = excluded.std_dev,\n                               updated_at = CURRENT_TIMESTAMP",
                        (
                            _var_var_12,
                            round(_var_var_134, 3),
                            len(_var_var_41),
                            _var_var_127,
                            round(_var_var_125, 3),
                        ),
                    )
        except Exception as _var_var_17:
            _LOGGER.error("Error saving hourly factors: %s", _var_var_17)
        if True:
            return

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
            _var_var_18 = await self.fetch_all(
                "SELECT hour, factor_type, factor_value, sample_count, confidence, std_dev\n                   FROM weather_precision_hourly_factors\n                   WHERE factor_type IN ('solar_radiation_wm2', 'clouds')"
            )
            _var_var_19 = {"solar_radiation_wm2": {}, "clouds": {}, "min_samples": 3}
            for _var_var_20 in _var_var_18:
                (
                    _var_var_12,
                    _var_var_136,
                    _var_var_137,
                    _var_var_138,
                    _var_var_127,
                    _var_var_125,
                ) = _var_var_20
                if _var_var_136 in _var_var_19:
                    _var_var_19[_var_var_136][str(_var_var_12)] = {
                        "factor": _var_var_137,
                        "samples": _var_var_138,
                        "confidence": _var_var_127 or 0.0,
                        "std_dev": _var_var_125,
                    }
            return _var_var_19
        except Exception as _var_var_17:
            _LOGGER.error("Error getting hourly correction factors: %s", _var_var_17)
            return {"solar_radiation_wm2": {}, "clouds": {}, "min_samples": 3}
