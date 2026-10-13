# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util, get_season
from ..astronomy_provider import AstronomyProviderAdapter
from ..data.data_shadow_detection import ShadowDetector


class MorningRoutineHandler:

    async def execute_morning_routine_with_retry(
        self,
        date: str,
        hourly_forecast: List[Dict[str, Any]],
        weather_hourly: List[Dict[str, Any]],
        astronomy_data: Dict[str, Any],
        sensor_config: Dict[str, Any],
    ) -> bool:
        """
        Execute morning routine with bulletproof retry logic.

        Returns:
            True if successful, False if all retries failed. @zara
        """
        if self.db is None:
            _LOGGER.error("Database not available for morning routine")
            return False
        _var_var_0 = 3
        try:
            for _var_var_1 in range(1, _var_var_0 + 1):
                _LOGGER.info("")
                _LOGGER.info(
                    "================================================================================"
                )
                _LOGGER.info(
                    "  MORNING ROUTINE EXECUTION - Attempt %d/%d for %s",
                    _var_var_1,
                    _var_var_0,
                    date,
                )
                _LOGGER.info(
                    "================================================================================"
                )
                try:
                    _LOGGER.info("→ Step 1/5: Validating prerequisites...")
                    if not await self._validate_prerequisites(
                        date, hourly_forecast, weather_hourly, astronomy_data
                    ):
                        raise ValueError("Prerequisites validation failed")
                    _LOGGER.info(
                        "  ✓ Prerequisites validated: %d forecast hours, %d weather hours",
                        len(hourly_forecast),
                        len(weather_hourly),
                    )
                    _var_var_2 = getattr(self.coordinator, "inverter_max_power", 0.0)
                    if _var_var_2 > 0:
                        _LOGGER.info(
                            "  ℹ Inverter capping enabled: %.2f kW", _var_var_2
                        )
                    _LOGGER.info(
                        "→ Step 2/5: Creating hourly predictions in database..."
                    )
                    _var_var_3 = await self._create_daily_predictions(
                        date=date,
                        hourly_forecast=hourly_forecast,
                        weather_forecast=weather_hourly,
                        astronomy_data=astronomy_data,
                        sensor_config=sensor_config,
                        inverter_max_power=_var_var_2,
                    )
                    _LOGGER.info("  ✓ Hourly predictions created successfully")
                    _LOGGER.info("→ Step 3/5: Verifying data integrity...")
                    if not await self._verify_integrity(date, _var_var_3):
                        raise RuntimeError("Integrity verification failed")
                    _LOGGER.info("  ✓ Data integrity verified - no duplicates found")
                    _LOGGER.info("→ Step 4/5: Refreshing coordinator cache...")
                    await self.coordinator._refresh_hourly_predictions_cache()
                    _LOGGER.info("  ✓ Coordinator cache refreshed")
                    _LOGGER.info("→ Step 5/5: Triggering sensor updates...")
                    await self.coordinator.async_request_refresh()
                    _LOGGER.info("  ✓ Sensor updates triggered")
                    _LOGGER.info("")
                    _LOGGER.info(
                        "================================================================================"
                    )
                    _LOGGER.info("  ✓✓✓ MORNING ROUTINE SUCCESSFUL ✓✓✓")
                    _LOGGER.info(
                        "  Date: %s | Attempt: %d/%d", date, _var_var_1, _var_var_0
                    )
                    _LOGGER.info(
                        "================================================================================"
                    )
                    _LOGGER.info("")
                    return True
                except Exception as _var_var_4:
                    _LOGGER.error(
                        "Morning routine attempt %d/%d FAILED: %s",
                        _var_var_1,
                        _var_var_0,
                        _var_var_4,
                        exc_info=True,
                    )
                    if _var_var_1 < _var_var_0:
                        _var_var_5 = 60 * _var_var_1
                        _LOGGER.info(
                            "Waiting %ds before retry %d...", _var_var_5, _var_var_1 + 1
                        )
                        await asyncio.sleep(_var_var_5)
                    else:
                        _LOGGER.error(
                            "CRITICAL: Morning routine FAILED after %d attempts! Manual intervention required",
                            _var_var_0,
                        )
                        await self._send_critical_alert(
                            "Morning routine failure", str(_var_var_4)
                        )
                        return False
            return False
        except Exception as _var_var_4:
            _LOGGER.error(
                "CRITICAL: Morning routine handler crashed: %s",
                _var_var_4,
                exc_info=True,
            )
            return False

    async def _create_daily_predictions(
        self,
        date: str,
        hourly_forecast: List[Dict[str, Any]],
        weather_forecast: List[Dict[str, Any]],
        astronomy_data: Dict[str, Any],
        sensor_config: Dict[str, Any],
        inverter_max_power: float = 0.0,
    ) -> set:
        """Create daily predictions in database. @zara

        Morning Routine UNLOCKS, updates predictions, and RE-LOCKS.
        This allows multiple runs per day (00:30 + sunrise-based).
        Raises on error so retry logic in caller can handle it.
        """
        if False:
            __assert_armored__ = None
        _LOGGER.info("  ├─ Checking and unlocking existing forecast locks...")
        _var_var_8 = await self.db.fetchone(
            "SELECT COUNT(*) as count FROM hourly_predictions WHERE target_date = ?",
            (date,),
        )
        _var_var_9 = _var_var_8[0] if _var_var_8 else 0
        if _var_var_9 > 0:
            _LOGGER.info(
                "  │  🔓 Found %d existing predictions - will update", _var_var_9
            )
        _var_var_10 = await self.db.fetchone(
            "SELECT locked, locked_at, source FROM daily_forecasts\n               WHERE forecast_type = 'today' AND forecast_date = ?",
            (date,),
        )
        if _var_var_10 and _var_var_10[0]:
            _LOGGER.info(
                "  │  🔓 Unlocking daily_forecast (was locked at %s by %s)",
                _var_var_10[1],
                _var_var_10[2],
            )
            await self.db.execute(
                "UPDATE daily_forecasts SET locked = 0\n                   WHERE forecast_type = 'today' AND forecast_date = ?",
                (date,),
            )
        _LOGGER.info("  ├─ Preparing to update predictions for 3 days...")
        from datetime import timedelta as _var_var_11

        _var_var_12 = datetime.fromisoformat(date).date()
        _var_var_13 = set()
        for _var_var_14 in hourly_forecast:
            _var_var_15 = _var_var_14.get("hour")
            _var_var_16 = _var_var_14.get("date")
            if _var_var_15 is not None and _var_var_16:
                _var_var_13.add(f"{_var_var_16}_{_var_var_15:02d}")
        _var_var_17 = {}
        _var_var_18 = {}
        _var_var_19 = {}
        _var_var_20 = {}
        if _var_var_13:
            _var_var_21 = ",".join(["?"] * len(_var_var_13))
            _var_var_22 = tuple(_var_var_13)
            _var_var_23 = await self.db.fetchall(
                f"SELECT prediction_id, actual_kwh, has_sensor_data\n                    FROM hourly_predictions\n                    WHERE prediction_id IN ({_var_var_21}) AND actual_kwh IS NOT NULL",
                _var_var_22,
            )
            for _var_var_24 in _var_var_23:
                _var_var_17[_var_var_24[0]] = {
                    "actual_kwh": _var_var_24[1],
                    "has_sensor_data": _var_var_24[2],
                }
            _var_var_25 = await self.db.fetchall(
                f"SELECT prediction_id, group_name, actual_kwh\n                    FROM prediction_panel_groups\n                    WHERE prediction_id IN ({_var_var_21}) AND actual_kwh IS NOT NULL",
                _var_var_22,
            )
            for _var_var_24 in _var_var_25:
                _var_var_18[_var_var_24[0], _var_var_24[1]] = _var_var_24[2]
            _var_var_26 = await self.db.fetchall(
                f"SELECT prediction_id, temperature_c, humidity_percent,\n                       solar_radiation_wm2, rain_mm, wind_speed_ms, current_yield_kwh\n                    FROM prediction_sensor_actual\n                    WHERE prediction_id IN ({_var_var_21})",
                _var_var_22,
            )
            for _var_var_24 in _var_var_26:
                _var_var_19[_var_var_24[0]] = {
                    "temperature_c": _var_var_24[1],
                    "humidity_percent": _var_var_24[2],
                    "solar_radiation_wm2": _var_var_24[3],
                    "rain_mm": _var_var_24[4],
                    "wind_speed_ms": _var_var_24[5],
                    "current_yield_kwh": _var_var_24[6],
                }
            _var_var_27 = await self.db.fetchall(
                f"SELECT prediction_id, temperature, solar_radiation_wm2, wind,\n                       humidity, rain, clouds, pressure, lux,\n                       frost_detected, frost_score, frost_confidence\n                    FROM prediction_weather\n                    WHERE prediction_id IN ({_var_var_21}) AND weather_type = 'actual'",
                _var_var_22,
            )
            for _var_var_24 in _var_var_27:
                _var_var_20[_var_var_24[0]] = {
                    "temperature": _var_var_24[1],
                    "solar_radiation_wm2": _var_var_24[2],
                    "wind": _var_var_24[3],
                    "humidity": _var_var_24[4],
                    "rain": _var_var_24[5],
                    "clouds": _var_var_24[6],
                    "pressure": _var_var_24[7],
                    "lux": _var_var_24[8],
                    "frost_detected": _var_var_24[9],
                    "frost_score": _var_var_24[10],
                    "frost_confidence": _var_var_24[11],
                }
            if _var_var_17:
                _LOGGER.info(
                    "  │  Preserved %d hourly actuals, %d group actuals before re-creation",
                    len(_var_var_17),
                    len(_var_var_18),
                )
            await self.db.execute(
                f"DELETE FROM hourly_predictions WHERE prediction_id IN ({_var_var_21})",
                _var_var_22,
            )
            _LOGGER.info(
                "  │  Cleared %d existing prediction slots for update", len(_var_var_13)
            )
        else:
            _LOGGER.warning("  │  No incoming predictions to update - skipping DELETE")
        _var_var_28 = dt_util.now()
        _var_var_29 = {}
        for _var_var_30 in weather_forecast:
            _var_var_31 = (_var_var_30.get("date", date), _var_var_30.get("hour"))
            _var_var_29[_var_var_31] = _var_var_30
        _LOGGER.info(
            "  ├─ Processing %d input forecast entries (3 days)...",
            len(hourly_forecast),
        )
        _var_var_32 = set()
        _var_var_33 = 0
        _var_var_34 = {}
        for _var_var_14 in hourly_forecast:
            _var_var_15 = _var_var_14.get("hour")
            if _var_var_15 is None:
                continue
            _var_var_16 = _var_var_14.get("date")
            if not _var_var_16:
                _var_var_16 = date
                _LOGGER.warning(
                    "  │  ⚠️  Missing date in hour_data for hour %d, using %s",
                    _var_var_15,
                    date,
                )
            _var_var_35 = f"{_var_var_16}_{_var_var_15:02d}"
            if _var_var_35 in _var_var_32:
                _var_var_33 += 1
                continue
            _var_var_32.add(_var_var_35)
            _var_var_34[_var_var_16] = _var_var_34.get(_var_var_16, 0) + 1
            _var_var_36 = _var_var_14.get("production_kwh", 0.0)
            if inverter_max_power > 0:
                _var_var_37 = inverter_max_power
                _var_var_38 = _var_var_36
                _var_var_36 = min(_var_var_36, _var_var_37)
            else:
                _var_var_38 = _var_var_36
            _var_var_39 = f"{_var_var_16}T{_var_var_15:02d}:00:00"
            _var_var_40 = datetime.fromisoformat(_var_var_16)
            await self.db.execute(
                "INSERT OR REPLACE INTO hourly_predictions\n                   (prediction_id, prediction_created_at, prediction_created_hour,\n                    target_datetime, target_date, target_hour,\n                    target_day_of_week, target_day_of_year, target_month, target_season,\n                    prediction_kwh, prediction_kwh_uncapped, prediction_method,\n                    ml_contribution_percent, confidence, is_production_hour,\n                    physics_kwh, ai_kwh, ai_confidence,\n                    lstm_kwh, ridge_kwh)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _var_var_35,
                    _var_var_28,
                    _var_var_28.hour,
                    _var_var_39,
                    _var_var_16,
                    _var_var_15,
                    _var_var_40.weekday(),
                    _var_var_40.timetuple().tm_yday,
                    _var_var_40.month,
                    get_season(_var_var_40.month),
                    round(_var_var_36, 4),
                    round(_var_var_38, 4),
                    _var_var_14.get("method", "physics"),
                    _var_var_14.get("ml_contribution", 0),
                    _var_var_14.get("confidence", 75.0),
                    6 <= _var_var_15 <= 20,
                    _var_var_14.get("physics_kwh"),
                    _var_var_14.get("ai_kwh"),
                    _var_var_14.get("ai_confidence"),
                    _var_var_14.get("lstm_kwh"),
                    _var_var_14.get("ridge_kwh"),
                ),
            )
            _var_var_41 = _var_var_14.get("groups")
            if _var_var_41:
                for _var_var_42 in _var_var_41:
                    _var_var_43 = _var_var_42.get("name")
                    _var_var_44 = _var_var_42.get("power_kwh", 0.0)
                    _var_var_45 = _var_var_42.get("physics_kwh")
                    _var_var_46 = _var_var_42.get("ai_kwh")
                    _var_var_47 = _var_var_42.get("lstm_kwh")
                    _var_var_48 = _var_var_42.get("ridge_kwh")
                    await self.db.execute(
                        "INSERT INTO prediction_panel_groups\n                           (prediction_id, group_name, prediction_kwh, physics_kwh, ai_kwh,\n                            lstm_kwh, ridge_kwh)\n                           VALUES (?, ?, ?, ?, ?, ?, ?)\n                           ON CONFLICT(prediction_id, group_name) DO UPDATE SET\n                               prediction_kwh = excluded.prediction_kwh,\n                               physics_kwh = excluded.physics_kwh,\n                               ai_kwh = excluded.ai_kwh,\n                               lstm_kwh = excluded.lstm_kwh,\n                               ridge_kwh = excluded.ridge_kwh",
                        (
                            _var_var_35,
                            _var_var_43,
                            round(_var_var_44, 4),
                            round(_var_var_45, 4) if _var_var_45 else None,
                            round(_var_var_46, 4) if _var_var_46 else None,
                            round(_var_var_47, 4) if _var_var_47 else None,
                            round(_var_var_48, 4) if _var_var_48 else None,
                        ),
                    )
                await self.db.execute(
                    "UPDATE hourly_predictions SET has_panel_group_predictions = TRUE\n                       WHERE prediction_id = ?",
                    (_var_var_35,),
                )
            _var_var_49 = _var_var_29.get((_var_var_16, _var_var_15), {})
            if _var_var_49:
                await self.db.execute(
                    "INSERT INTO prediction_weather\n                       (prediction_id, weather_type, temperature, solar_radiation_wm2,\n                        wind, humidity, rain, clouds, pressure, source, diffuse_radiation,\n                        direct_radiation)\n                       VALUES (?, 'forecast', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _var_var_35,
                        _var_var_49.get("temperature"),
                        _var_var_49.get("solar_radiation_wm2"),
                        _var_var_49.get("wind"),
                        _var_var_49.get("humidity"),
                        _var_var_49.get("rain"),
                        _var_var_49.get("clouds"),
                        _var_var_49.get("pressure"),
                        _var_var_49.get("source", "blended"),
                        _var_var_49.get("diffuse_radiation") or _var_var_49.get("dhi"),
                        _var_var_49.get("direct_radiation"),
                    ),
                )
            _var_var_50 = astronomy_data.get(f"{_var_var_16}_{_var_var_15:02d}", {})
            if not _var_var_50:
                _var_var_50 = astronomy_data.get(str(_var_var_15), {})
            _var_var_51 = astronomy_data.get(_var_var_16, astronomy_data)
            if _var_var_50:
                _var_var_52 = _var_var_51.get("sunrise_local")
                _var_var_53 = _var_var_51.get("sunset_local")
                _var_var_54 = _var_var_51.get("solar_noon_local")
                _var_var_55 = _var_var_51.get("daylight_hours", 12.0)
                _var_var_56 = None
                _var_var_57 = None
                _var_var_58 = None
                _var_var_59 = None
                try:
                    from datetime import datetime as _var_var_6

                    _var_var_7 = _var_var_6(
                        _var_var_40.year,
                        _var_var_40.month,
                        _var_var_40.day,
                        _var_var_15,
                        30,
                    )

                    def _parse_ts(ts):
                        if ts is None:
                            return
                        if isinstance(ts, _var_var_6):
                            return ts.replace(
                                year=_var_var_7.year,
                                month=_var_var_7.month,
                                day=_var_var_7.day,
                                tzinfo=None,
                            )
                        _var_var_81 = str(ts).strip()
                        if "T" in _var_var_81 or len(_var_var_81) > 10:
                            try:
                                _var_var_82 = _var_var_6.fromisoformat(
                                    _var_var_81.replace("Z", "+00:00")
                                )
                                return _var_var_82.replace(
                                    year=_var_var_7.year,
                                    month=_var_var_7.month,
                                    day=_var_var_7.day,
                                    tzinfo=None,
                                )
                            except (ValueError, TypeError):
                                pass
                        for _var_var_83 in ("%H:%M:%S", "%H:%M"):
                            try:
                                _var_var_84 = _var_var_6.strptime(
                                    _var_var_81, _var_var_83
                                )
                                return _var_var_7.replace(
                                    hour=_var_var_84.hour,
                                    minute=_var_var_84.minute,
                                    second=_var_var_84.second,
                                )
                            except (ValueError, TypeError):
                                continue

                    _var_var_61 = _parse_ts(_var_var_52)
                    _var_var_62 = _parse_ts(_var_var_53)
                    _var_var_63 = _parse_ts(_var_var_54)
                    if _var_var_61 and _var_var_62:
                        _var_var_64 = (_var_var_62 - _var_var_61).total_seconds()
                        _var_var_65 = (_var_var_7 - _var_var_61).total_seconds()
                        _var_var_57 = (
                            max(0.0, min(1.0, _var_var_65 / _var_var_64))
                            if _var_var_64 > 0
                            else 0.5
                        )
                        _var_var_58 = max(0.0, _var_var_65 / 3600.0)
                        _var_var_59 = max(
                            0.0, (_var_var_62 - _var_var_7).total_seconds() / 3600.0
                        )
                    if _var_var_63:
                        _var_var_56 = (
                            _var_var_7 - _var_var_63
                        ).total_seconds() / 3600.0
                except Exception:
                    pass
                await self.db.execute(
                    "INSERT INTO prediction_astronomy\n                       (prediction_id, sunrise, sunset, solar_noon, daylight_hours,\n                        sun_elevation_deg, sun_azimuth_deg, clear_sky_radiation_wm2,\n                        theoretical_max_kwh, hours_since_solar_noon, day_progress_ratio,\n                        hours_after_sunrise, hours_before_sunset)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id) DO UPDATE SET\n                           sunrise = excluded.sunrise,\n                           sunset = excluded.sunset,\n                           solar_noon = excluded.solar_noon,\n                           daylight_hours = excluded.daylight_hours,\n                           sun_elevation_deg = excluded.sun_elevation_deg,\n                           sun_azimuth_deg = excluded.sun_azimuth_deg,\n                           clear_sky_radiation_wm2 = excluded.clear_sky_radiation_wm2,\n                           theoretical_max_kwh = excluded.theoretical_max_kwh,\n                           hours_since_solar_noon = excluded.hours_since_solar_noon,\n                           day_progress_ratio = excluded.day_progress_ratio,\n                           hours_after_sunrise = excluded.hours_after_sunrise,\n                           hours_before_sunset = excluded.hours_before_sunset",
                    (
                        _var_var_35,
                        _var_var_52,
                        _var_var_53,
                        _var_var_54,
                        _var_var_55,
                        _var_var_50.get("sun_elevation_deg"),
                        _var_var_50.get("sun_azimuth_deg"),
                        _var_var_50.get("clear_sky_radiation_wm2"),
                        _var_var_50.get("theoretical_max_kwh"),
                        _var_var_56,
                        _var_var_57,
                        _var_var_58,
                        _var_var_59,
                    ),
                )
                continue
        _var_var_66 = 0
        if _var_var_17:
            for _var_var_67, _var_var_68 in _var_var_17.items():
                await self.db.execute(
                    "UPDATE hourly_predictions\n                       SET actual_kwh = ?, has_sensor_data = ?\n                       WHERE prediction_id = ?",
                    (
                        _var_var_68["actual_kwh"],
                        _var_var_68["has_sensor_data"],
                        _var_var_67,
                    ),
                )
                _var_var_66 += 1
        if _var_var_18:
            for (_var_var_67, _var_var_43), _var_var_69 in _var_var_18.items():
                await self.db.execute(
                    "UPDATE prediction_panel_groups\n                       SET actual_kwh = ?\n                       WHERE prediction_id = ? AND group_name = ?",
                    (_var_var_69, _var_var_67, _var_var_43),
                )
        if _var_var_19:
            for _var_var_67, _var_var_70 in _var_var_19.items():
                await self.db.execute(
                    "INSERT INTO prediction_sensor_actual\n                       (prediction_id, temperature_c, humidity_percent,\n                        solar_radiation_wm2, rain_mm, wind_speed_ms, current_yield_kwh)\n                       VALUES (?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id) DO UPDATE SET\n                           temperature_c = excluded.temperature_c,\n                           humidity_percent = excluded.humidity_percent,\n                           solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                           rain_mm = excluded.rain_mm,\n                           wind_speed_ms = excluded.wind_speed_ms,\n                           current_yield_kwh = excluded.current_yield_kwh",
                    (
                        _var_var_67,
                        _var_var_70["temperature_c"],
                        _var_var_70["humidity_percent"],
                        _var_var_70["solar_radiation_wm2"],
                        _var_var_70["rain_mm"],
                        _var_var_70["wind_speed_ms"],
                        _var_var_70["current_yield_kwh"],
                    ),
                )
        if _var_var_20:
            for _var_var_67, _var_var_70 in _var_var_20.items():
                await self.db.execute(
                    "INSERT INTO prediction_weather\n                       (prediction_id, weather_type, temperature, solar_radiation_wm2,\n                        wind, humidity, rain, clouds, pressure, lux,\n                        frost_detected, frost_score, frost_confidence)\n                       VALUES (?, 'actual', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(prediction_id, weather_type) DO UPDATE SET\n                           temperature = excluded.temperature,\n                           solar_radiation_wm2 = excluded.solar_radiation_wm2,\n                           wind = excluded.wind,\n                           humidity = excluded.humidity,\n                           rain = excluded.rain,\n                           clouds = excluded.clouds,\n                           pressure = excluded.pressure,\n                           lux = excluded.lux,\n                           frost_detected = excluded.frost_detected,\n                           frost_score = excluded.frost_score,\n                           frost_confidence = excluded.frost_confidence",
                    (
                        _var_var_67,
                        _var_var_70["temperature"],
                        _var_var_70["solar_radiation_wm2"],
                        _var_var_70["wind"],
                        _var_var_70["humidity"],
                        _var_var_70["rain"],
                        _var_var_70["clouds"],
                        _var_var_70["pressure"],
                        _var_var_70["lux"],
                        _var_var_70["frost_detected"],
                        _var_var_70["frost_score"],
                        _var_var_70["frost_confidence"],
                    ),
                )
        if _var_var_66 > 0:
            _LOGGER.info(
                "  ├─ Restored %d hourly actuals, %d group actuals, %d sensor, %d weather actuals",
                _var_var_66,
                len(_var_var_18),
                len(_var_var_19),
                len(_var_var_20),
            )
        _LOGGER.info(
            "  ├─ Calculating daily totals from saved predictions for 3 days..."
        )
        _var_var_71 = {}
        for _var_var_72 in range(3):
            _var_var_73 = (_var_var_12 + _var_var_11(days=_var_var_72)).isoformat()
            _var_var_74 = await self.db.fetchone(
                "SELECT SUM(prediction_kwh) as total FROM hourly_predictions WHERE target_date = ?",
                (_var_var_73,),
            )
            _var_var_75 = (
                _var_var_74["total"] if _var_var_74 and _var_var_74["total"] else 0.0
            )
            _var_var_71[_var_var_73] = _var_var_75
            _LOGGER.info("  │  → %s: %.2f kWh", _var_var_73, _var_var_75)
        _LOGGER.info("  ├─ Saving daily forecasts to database for 3 days...")
        _var_var_76 = ["today", "tomorrow", "day_after_tomorrow"]
        for _var_var_72, _var_var_77 in enumerate(_var_var_76):
            _var_var_73 = (_var_var_12 + _var_var_11(days=_var_var_72)).isoformat()
            _var_var_75 = _var_var_71.get(_var_var_73, 0.0)
            await self.db.execute(
                "INSERT INTO daily_forecasts\n                   (forecast_type, forecast_date, prediction_kwh, prediction_kwh_raw,\n                    locked, locked_at, source, created_at)\n                   VALUES (?, ?, ?, ?, TRUE, ?, 'morning_routine', ?)\n                   ON CONFLICT(forecast_type, forecast_date) DO UPDATE SET\n                       prediction_kwh = excluded.prediction_kwh,\n                       prediction_kwh_raw = excluded.prediction_kwh_raw,\n                       locked = excluded.locked,\n                       locked_at = excluded.locked_at,\n                       source = excluded.source",
                (
                    _var_var_77,
                    _var_var_73,
                    round(_var_var_75, 2),
                    round(_var_var_75, 2),
                    _var_var_28,
                    _var_var_28,
                ),
            )
        _LOGGER.debug(
            "     → Daily forecasts LOCKED for 3 days: locked=TRUE, locked_at=%s",
            _var_var_28,
        )
        _var_var_75 = _var_var_71.get(date, 0.0)
        _LOGGER.info("  ├─ Updating coordinator expected daily production...")
        await self.coordinator.data_manager.save_expected_daily_production(
            round(_var_var_75, 2)
        )
        self.coordinator.expected_daily_production = round(_var_var_75, 2)
        _LOGGER.info("  ├─ Setting morning routine lock for %s...", date)
        await self.db.execute(
            "UPDATE coordinator_state\n               SET last_set_date = ?, last_updated = ?\n               WHERE id = 1",
            (date, _var_var_28),
        )
        _LOGGER.debug("     → Lock flag set: last_set_date = %s", date)
        _var_var_78 = len(_var_var_32)
        _LOGGER.info("  └─ SUMMARY:")
        _LOGGER.info("     • Input entries: %d (3 days)", len(hourly_forecast))
        _LOGGER.info("     • Created predictions: %d", _var_var_78)
        if _var_var_33 > 0:
            _LOGGER.info("     • Skipped duplicates: %d", _var_var_33)
        _LOGGER.info("     • Predictions per day:")
        for _var_var_16, _var_var_79 in sorted(_var_var_34.items()):
            _var_var_80 = _var_var_71.get(_var_var_16, 0.0)
            _LOGGER.info(
                "       - %s: %d hours, %.2f kWh", _var_var_16, _var_var_79, _var_var_80
            )
        _LOGGER.info("     • Coordinator updated: Yes (today=%.2f kWh)", _var_var_75)
        return set(_var_var_34.keys())

    async def _verify_integrity(self, date: str, created_dates: set = None) -> bool:
        """Verify data integrity after creation. @zara"""
        try:
            if created_dates:
                _var_var_85 = sorted(created_dates)
            else:
                from datetime import timedelta as _var_var_86

                _var_var_87 = datetime.fromisoformat(date).date()
                _var_var_85 = [
                    (_var_var_87 + _var_var_86(days=_var_var_88)).isoformat()
                    for _var_var_88 in range(3)
                ]
            for _var_var_89 in _var_var_85:
                _var_var_24 = await self.db.fetchone(
                    "SELECT COUNT(*) FROM hourly_predictions\n                       WHERE target_date = ?",
                    (_var_var_89,),
                )
                if not _var_var_24 or _var_var_24[0] == 0:
                    _LOGGER.error(
                        "No predictions found for %s after creation!", _var_var_89
                    )
                    return False
                _var_var_90 = await self.db.fetchone(
                    "SELECT prediction_id, COUNT(*) as cnt\n                       FROM hourly_predictions\n                       WHERE target_date = ?\n                       GROUP BY prediction_id\n                       HAVING cnt > 1",
                    (_var_var_89,),
                )
                if _var_var_90:
                    _LOGGER.error(
                        "Duplicate prediction IDs found for %s: %s",
                        _var_var_89,
                        _var_var_90[0],
                    )
                    return False
            _LOGGER.debug(
                "Integrity verified for %d days starting from %s, no duplicates",
                len(_var_var_85),
                _var_var_85[0] if _var_var_85 else date,
            )
        except Exception as _var_var_4:
            _LOGGER.error(
                "Integrity verification failed: %s", _var_var_4, exc_info=True
            )
            return False
        return True
