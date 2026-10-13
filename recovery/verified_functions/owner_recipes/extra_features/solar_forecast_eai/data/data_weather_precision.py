# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from .data_io import DataManagerIO


class WeatherPrecisionTracker(DataManagerIO):

    async def _update_rolling_averages(self) -> None:
        """Update rolling average correction factors with hourly + weather-specific. @zara

        V16.1 enhanced: Now calculates:
        - Daily rolling averages (as before)
        - Hourly factors for solar_radiation_wm2 and clouds (hours 7-17)
        - Weather-specific factors for clear/cloudy days
        """
        import math as _var_var_60

        try:
            _var_var_61 = 30
            _var_var_62 = 50
            _var_var_63 = 3
            _var_var_64 = 2
            _var_var_65 = (
                datetime.now() - timedelta(days=ROLLING_WINDOW_DAYS)
            ).strftime("%Y-%m-%d")
            _var_var_66 = await self.fetch_all(
                "SELECT ds.date\n                   FROM weather_precision_daily_summary ds\n                   WHERE ds.date >= ?\n                     AND ds.avg_solar_factor = 1.0\n                     AND ds.avg_temp_offset = 0.0",
                (_var_var_65,),
            )
            if _var_var_66:
                for _var_var_67 in _var_var_66:
                    _var_var_68 = str(_var_var_67[0])
                    _var_var_69 = await self.fetch_all(
                        "SELECT solar_factor, temp_offset, clouds_factor,\n                                  wind_factor, humidity_factor, rain_difference,\n                                  pressure_offset\n                           FROM weather_precision_daily\n                           WHERE date = ? AND solar_factor IS NOT NULL",
                        (_var_var_68,),
                    )
                    if _var_var_69:
                        _var_var_71 = [
                            _var_var_70[0]
                            for _var_var_70 in _var_var_69
                            if _var_var_70[0] is not None
                            if SOLAR_FACTOR_MIN <= _var_var_70[0] <= SOLAR_FACTOR_MAX
                        ]
                        _var_var_72 = [
                            _var_var_70[1]
                            for _var_var_70 in _var_var_69
                            if _var_var_70[1] is not None
                            if -20 <= _var_var_70[1] <= 20
                        ]
                        _var_var_73 = [
                            _var_var_70[2]
                            for _var_var_70 in _var_var_69
                            if _var_var_70[2] is not None
                            if 0.1 <= _var_var_70[2] <= 10.0
                        ]
                        _var_var_74 = [
                            _var_var_70[3]
                            for _var_var_70 in _var_var_69
                            if _var_var_70[3] is not None
                            if 0.1 <= _var_var_70[3] <= 10.0
                        ]
                        _var_var_75 = [
                            _var_var_70[4]
                            for _var_var_70 in _var_var_69
                            if _var_var_70[4] is not None
                            if 0.1 <= _var_var_70[4] <= 10.0
                        ]
                        _var_var_76 = [
                            _var_var_70[5]
                            for _var_var_70 in _var_var_69
                            if _var_var_70[5] is not None
                        ]
                        _var_var_77 = [
                            _var_var_70[6]
                            for _var_var_70 in _var_var_69
                            if _var_var_70[6] is not None
                        ]
                        await self.execute_query(
                            "UPDATE weather_precision_daily_summary\n                               SET avg_solar_factor = ?,\n                                   avg_temp_offset = ?,\n                                   avg_clouds_factor = ?,\n                                   avg_wind_factor = ?,\n                                   avg_humidity_factor = ?,\n                                   avg_rain_diff = ?,\n                                   avg_pressure_offset = ?\n                               WHERE date = ?",
                            (
                                (
                                    round(sum(_var_var_71) / len(_var_var_71), 3)
                                    if _var_var_71
                                    else 1.0
                                ),
                                (
                                    round(sum(_var_var_72) / len(_var_var_72), 2)
                                    if _var_var_72
                                    else 0.0
                                ),
                                (
                                    round(sum(_var_var_73) / len(_var_var_73), 3)
                                    if _var_var_73
                                    else 1.0
                                ),
                                (
                                    round(sum(_var_var_74) / len(_var_var_74), 3)
                                    if _var_var_74
                                    else 1.0
                                ),
                                (
                                    round(sum(_var_var_75) / len(_var_var_75), 3)
                                    if _var_var_75
                                    else 1.0
                                ),
                                (
                                    round(sum(_var_var_76) / len(_var_var_76), 2)
                                    if _var_var_76
                                    else 0.0
                                ),
                                (
                                    round(sum(_var_var_77) / len(_var_var_77), 2)
                                    if _var_var_77
                                    else 0.0
                                ),
                                _var_var_68,
                            ),
                        )
                        continue
                _LOGGER.info(
                    "Repaired %d daily_summary rows from hourly data", len(_var_var_66)
                )
            _var_var_78 = await self.fetch_all(
                "SELECT date, hour, solar_forecast, solar_actual, solar_factor,\n                          clouds_forecast, clouds_actual, clouds_factor,\n                          temp_forecast, temp_actual, temp_offset,\n                          humidity_forecast, humidity_actual, humidity_factor,\n                          wind_forecast, wind_actual, wind_factor,\n                          rain_forecast, rain_actual, rain_difference,\n                          pressure_forecast, pressure_actual, pressure_offset\n                   FROM weather_precision_daily\n                   WHERE date >= ?\n                   ORDER BY date, hour",
                (_var_var_65,),
            )
            _var_var_79 = await self.fetch_all(
                "SELECT date, hours_tracked, avg_temp_offset, avg_solar_factor,\n                          avg_clouds_factor, avg_wind_factor, avg_humidity_factor,\n                          avg_rain_diff, avg_pressure_offset\n                   FROM weather_precision_daily_summary\n                   WHERE date >= ?\n                   ORDER BY date DESC",
                (_var_var_65,),
            )
            if not _var_var_79:
                return
            _var_var_80 = {}
            for _var_var_19 in _var_var_78:
                _var_var_81 = str(_var_var_19[0])
                if _var_var_81 not in _var_var_80:
                    _var_var_80[_var_var_81] = []
                _var_var_80[_var_var_81].append(_var_var_19)
            _var_var_82 = {
                "temperature": [],
                "solar_radiation_wm2": [],
                "clouds": [],
                "humidity": [],
                "wind": [],
                "rain": [],
                "pressure": [],
            }
            _var_var_83 = {_var_var_5: [] for _var_var_5 in _var_var_82.keys()}
            _var_var_84 = {_var_var_5: [] for _var_var_5 in _var_var_82.keys()}
            _var_var_85 = {}
            _var_var_86 = {}
            _var_var_87 = 0
            _var_var_88 = 0
            _var_var_89 = 0
            for _var_var_90 in _var_var_79:
                _var_var_81 = str(_var_var_90[0])
                _var_var_91 = _var_var_90[1] or 0
                if _var_var_91 < MIN_HOURS_FOR_FACTOR:
                    continue
                _var_var_92 = _var_var_80.get(_var_var_81, [])
                _var_var_93 = []
                for _var_var_94 in _var_var_92:
                    _var_var_12 = _var_var_94[1]
                    _var_var_56 = _var_var_94[5]
                    if _var_var_12 is not None:
                        if 7 <= _var_var_12 <= 17:
                            if _var_var_56 is not None:
                                _var_var_93.append(_var_var_56)
                _var_var_95 = None
                if _var_var_93:
                    _var_var_95 = sum(_var_var_93) / len(_var_var_93)
                _var_var_96 = _var_var_95 is not None and _var_var_95 <= _var_var_61
                _var_var_97 = _var_var_95 is not None and _var_var_95 >= _var_var_62
                _var_var_48 = _var_var_90[2]
                _var_var_55 = _var_var_90[3]
                _var_var_98 = _var_var_90[4]
                _var_var_52 = _var_var_90[5]
                _var_var_51 = _var_var_90[6]
                _var_var_99 = _var_var_90[7]
                _var_var_54 = _var_var_90[8]
                if _var_var_48 is not None:
                    _var_var_82["temperature"].append(_var_var_48)
                    if _var_var_96:
                        _var_var_83["temperature"].append(_var_var_48)
                    elif _var_var_97:
                        _var_var_84["temperature"].append(_var_var_48)
                if _var_var_55 is not None:
                    _var_var_82["solar_radiation_wm2"].append(_var_var_55)
                    if _var_var_96:
                        _var_var_83["solar_radiation_wm2"].append(_var_var_55)
                    elif _var_var_97:
                        _var_var_84["solar_radiation_wm2"].append(_var_var_55)
                if _var_var_98 is not None:
                    _var_var_82["clouds"].append(_var_var_98)
                    if _var_var_96:
                        _var_var_83["clouds"].append(_var_var_98)
                    elif _var_var_97:
                        _var_var_84["clouds"].append(_var_var_98)
                if _var_var_52 is not None:
                    _var_var_82["wind"].append(_var_var_52)
                if _var_var_51 is not None:
                    _var_var_82["humidity"].append(_var_var_51)
                if _var_var_99 is not None:
                    _var_var_82["rain"].append(_var_var_99)
                if _var_var_54 is not None:
                    _var_var_82["pressure"].append(_var_var_54)
                _var_var_87 += 1
                if _var_var_96:
                    _var_var_88 += 1
                elif _var_var_97:
                    _var_var_89 += 1
                for _var_var_94 in _var_var_92:
                    _var_var_12 = _var_var_94[1]
                    if _var_var_12 is None or not 7 <= _var_var_12 <= 17:
                        continue
                    _var_var_100 = _var_var_94[4]
                    _var_var_101 = _var_var_94[7]
                    if _var_var_100 is not None:
                        if SOLAR_FACTOR_MIN <= _var_var_100 <= SOLAR_FACTOR_MAX:
                            if _var_var_12 not in _var_var_85:
                                _var_var_85[_var_var_12] = []
                            _var_var_85[_var_var_12].append(_var_var_100)
                    if _var_var_101 is not None:
                        if 0.1 <= _var_var_101 <= 5.0:
                            if _var_var_12 not in _var_var_86:
                                _var_var_86[_var_var_12] = []
                            _var_var_86[_var_var_12].append(_var_var_101)
                continue
            _var_var_102 = {}
            _var_var_103 = {}
            _var_var_104 = ["temperature", "pressure"]
            for _var_var_43, _var_var_46 in _var_var_82.items():
                if _var_var_46 and len(_var_var_46) >= 3:
                    _var_var_105 = sum(_var_var_46) / len(_var_var_46)
                    _var_var_102[_var_var_43] = round(_var_var_105, 3)
                    _var_var_106 = min(1.0, len(_var_var_46) / 7)
                    _var_var_59 = _var_var_105
                    _var_var_107 = sum(
                        (
                            (_var_var_111 - _var_var_59) ** 2
                            for _var_var_111 in _var_var_46
                        )
                    ) / len(_var_var_46)
                    _var_var_108 = _var_var_60.sqrt(_var_var_107)
                    _var_var_109 = max(0.0, 1.0 - _var_var_108 * 2)
                    _var_var_110 = (_var_var_106 + _var_var_109) / 2
                    _var_var_103[_var_var_43] = round(_var_var_110, 2)
                else:
                    if _var_var_43 in _var_var_104:
                        _var_var_102[_var_var_43] = 0.0
                    else:
                        _var_var_102[_var_var_43] = 1.0
                    _var_var_103[_var_var_43] = 0.0
            await self.execute_query(
                "INSERT INTO weather_precision_factors\n                   (id, temperature_factor, solar_factor, cloud_factor,\n                    wind_factor, humidity_factor, rain_factor, pressure_factor,\n                    sample_days, confidence, updated_at)\n                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n                   ON CONFLICT(id) DO UPDATE SET\n                       temperature_factor = excluded.temperature_factor,\n                       solar_factor = excluded.solar_factor,\n                       cloud_factor = excluded.cloud_factor,\n                       wind_factor = excluded.wind_factor,\n                       humidity_factor = excluded.humidity_factor,\n                       rain_factor = excluded.rain_factor,\n                       pressure_factor = excluded.pressure_factor,\n                       sample_days = excluded.sample_days,\n                       confidence = excluded.confidence,\n                       updated_at = CURRENT_TIMESTAMP",
                (
                    _var_var_102.get("temperature", 0.0),
                    _var_var_102.get("solar_radiation_wm2", 1.0),
                    _var_var_102.get("clouds", 1.0),
                    _var_var_102.get("wind", 1.0),
                    _var_var_102.get("humidity", 1.0),
                    1.0,
                    _var_var_102.get("pressure", 0.0),
                    _var_var_87,
                    _var_var_103.get("solar_radiation_wm2", 0.0),
                ),
            )
            await self._save_hourly_factors(_var_var_85, _var_var_86, _var_var_63)
            await self._save_weather_specific_factors(
                _var_var_83, _var_var_84, _var_var_88, _var_var_89, _var_var_64
            )
            _LOGGER.info(
                "Updated rolling factors: %d days (clear=%d, cloudy=%d), solar=%.3f, hourly_hours=%d",
                _var_var_87,
                _var_var_88,
                _var_var_89,
                _var_var_102.get("solar_radiation_wm2", 1.0),
                len(_var_var_85),
            )
        except Exception as _var_var_8:
            _LOGGER.error(
                "Error updating rolling averages: %s", _var_var_8, exc_info=True
            )
        if True:
            return None

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
            _var_var_17 = await self.fetch_all(
                "SELECT hour, factor_type, factor_value, sample_count, confidence, std_dev\n                   FROM weather_precision_hourly_factors\n                   WHERE factor_type IN ('solar_radiation_wm2', 'clouds')"
            )
            _var_var_18 = {"solar_radiation_wm2": {}, "clouds": {}, "min_samples": 3}
            for _var_var_19 in _var_var_17:
                (
                    _var_var_12,
                    _var_var_113,
                    _var_var_114,
                    _var_var_115,
                    _var_var_110,
                    _var_var_108,
                ) = _var_var_19
                if _var_var_113 in _var_var_18:
                    _var_var_18[_var_var_113][str(_var_var_12)] = {
                        "factor": _var_var_114,
                        "samples": _var_var_115,
                        "confidence": _var_var_110 or 0.0,
                        "std_dev": _var_var_108,
                    }
            return _var_var_18
        except Exception as _var_var_8:
            _LOGGER.error("Error getting hourly correction factors: %s", _var_var_8)
            return {"solar_radiation_wm2": {}, "clouds": {}, "min_samples": 3}
