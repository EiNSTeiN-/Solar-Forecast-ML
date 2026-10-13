# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from .data_io import DataManagerIO
from .db_manager import DatabaseManager
from .data_shadow_detection import ShadowDetector
from .data_learning_filter import should_exclude_group_from_learning
from ..core.core_helpers import get_season


class HourlyPredictionsHandler(DataManagerIO):

    async def update_hourly_actual(
        self,
        date: str,
        hour: int,
        actual_kwh: float,
        sensor_data: Dict[str, Any],
        weather_actual: Optional[Dict[str, Any]] = None,
        astronomy_update: Optional[Dict[str, Any]] = None,
        inverter_max_power: float = 0.0,
        panel_group_actuals: Optional[Dict[str, float]] = None,
        zero_export_mode: bool = False,
        has_battery: bool = False,
        solar_to_battery_power: Optional[float] = None,
    ) -> bool:
        """Update actual values for a specific hour. @zara

        Called every hour (e.g. at 12:05 to update 11:00-12:00).

        Args:
            date: Date string YYYY-MM-DD
            hour: Hour (0-23)
            actual_kwh: Actual production in kWh
            sensor_data: Sensor readings
            weather_actual: Actual weather data
            astronomy_update: Updated astronomy data
            inverter_max_power: Max AC power for clipping detection
            panel_group_actuals: Actual per-group production
            zero_export_mode: Whether zero export is active
            has_battery: Whether system has battery
            solar_to_battery_power: Power flowing to battery (W)

        Returns:
            True if successful
        """
        try:
            await self.ensure_initialized()
            _var_var_6 = f"{date}_{hour:02d}"
            _var_var_13 = dt_util.now()
            _var_var_14 = await self.db.fetchone(
                "SELECT prediction_kwh FROM hourly_predictions WHERE prediction_id = ?",
                (_var_var_6,),
            )
            if not _var_var_14:
                _LOGGER.warning("No prediction found for %s", _var_var_6)
                return False
            _var_var_15 = _var_var_14[0]
            _var_var_16 = await self._get_prediction_weather(_var_var_6, "forecast")
            _var_var_17, _var_var_18, _var_var_19 = self._calculate_accuracy(
                _var_var_15, actual_kwh
            )
            _var_var_20 = False
            if inverter_max_power > 0:
                _var_var_21 = inverter_max_power * 0.95
                _var_var_20 = actual_kwh >= _var_var_21
            _var_var_22 = False
            _var_var_23 = None
            if zero_export_mode:
                _var_var_22, _var_var_23 = self._detect_mppt_throttle(
                    actual_kwh, has_battery, solar_to_battery_power
                )
            _var_var_24, _var_var_25 = self._detect_weather_alert(
                weather_actual, _var_var_16
            )
            _var_var_26 = _var_var_20 or _var_var_22 or _var_var_24
            await self.db.execute(
                "UPDATE hourly_predictions SET\n                    actual_kwh = ?,\n                    actual_measured_at = ?,\n                    accuracy_percent = ?,\n                    error_kwh = ?,\n                    error_percent = ?,\n                    inverter_clipped = ?,\n                    has_sensor_data = TRUE,\n                    sensor_data_complete = ?,\n                    exclude_from_learning = ?,\n                    mppt_throttled = ?,\n                    mppt_throttle_reason = ?,\n                    has_weather_alert = ?,\n                    weather_alert_type = ?\n                WHERE prediction_id = ?",
                (
                    round(actual_kwh, 4),
                    _var_var_13.isoformat(),
                    _var_var_17,
                    _var_var_18,
                    _var_var_19,
                    _var_var_20,
                    self._check_sensor_completeness(sensor_data),
                    _var_var_26,
                    _var_var_22,
                    _var_var_23,
                    _var_var_24,
                    _var_var_25,
                    _var_var_6,
                ),
            )
            await self._save_sensor_actual(_var_var_6, sensor_data)
            if weather_actual:
                await self._save_prediction_weather(
                    _var_var_6, "actual", weather_actual
                )
            if astronomy_update:
                await self._update_prediction_astronomy(_var_var_6, astronomy_update)
            if panel_group_actuals:
                for _var_var_27, _var_var_28 in panel_group_actuals.items():
                    await self.db.execute(
                        "UPDATE prediction_panel_groups\n                           SET actual_kwh = ?\n                           WHERE prediction_id = ? AND group_name = ?",
                        (round(_var_var_28, 4), _var_var_6, _var_var_27),
                    )
                    _var_var_29 = await self.db.fetchone(
                        "SELECT prediction_kwh FROM prediction_panel_groups\n                           WHERE prediction_id = ? AND group_name = ?",
                        (_var_var_6, _var_var_27),
                    )
                    if _var_var_29:
                        _var_var_30 = _var_var_29[0]
                        _var_var_31, _var_var_32, _var_var_33 = (
                            self._calculate_accuracy(_var_var_30, _var_var_28)
                        )
                        _var_var_34 = {
                            "prediction_kwh": _var_var_30,
                            "actual_kwh": _var_var_28,
                            "error_kwh": _var_var_32,
                            "error_percent": _var_var_33,
                            "accuracy_percent": _var_var_31,
                        }
                        await self.db.save_hourly_panel_group_accuracy(
                            _var_var_6, _var_var_27, _var_var_34
                        )
                await self.db.execute(
                    "UPDATE hourly_predictions\n                       SET has_panel_group_actuals = TRUE\n                       WHERE prediction_id = ?",
                    (_var_var_6,),
                )
            _var_var_35 = await self._get_prediction_astronomy(_var_var_6)
            _var_var_36 = {
                "actual_kwh": actual_kwh,
                "astronomy": _var_var_35,
                "sensor_actual": sensor_data,
                "weather_actual": weather_actual,
                "weather_forecast": _var_var_16,
            }
            _var_var_37 = await self._shadow_detector.detect_shadow_ensemble(
                _var_var_36
            )
            await self.db.save_hourly_shadow_detection(_var_var_6, _var_var_37)
            if panel_group_actuals:
                _var_var_38 = {}
                for _var_var_39, _var_var_40 in panel_group_actuals.items():
                    _var_var_41 = await self.db.fetchone(
                        "SELECT physics_kwh FROM prediction_panel_groups\n                           WHERE prediction_id = ? AND group_name = ?",
                        (_var_var_6, _var_var_39),
                    )
                    _var_var_38[_var_var_39] = {
                        "actual_kwh": _var_var_40,
                        "theoretical_max_kwh": (
                            _var_var_41[0] if _var_var_41 and _var_var_41[0] else 0.0
                        ),
                    }
                _var_var_42 = await self._shadow_detector.detect_shadow_per_group(
                    {**_var_var_36, "prediction_id": _var_var_6},
                    _var_var_38,
                    system_ensemble=_var_var_37,
                )
                _var_var_43 = (
                    weather_actual.get("snow_per_group", {}) if weather_actual else {}
                )
                if not _var_var_43:
                    try:
                        _var_var_43 = await self.db.load_snow_tracking_groups()
                    except Exception:
                        _var_var_43 = {}
                _var_var_44 = {
                    "flags": {
                        "inverter_clipped": _var_var_20,
                        "mppt_throttled": _var_var_22,
                        "mppt_throttle_reason": _var_var_23,
                        "has_weather_alert": _var_var_24,
                        "weather_alert_type": _var_var_25,
                    }
                }
                for _var_var_39 in panel_group_actuals:
                    _var_var_45, _var_var_46 = should_exclude_group_from_learning(
                        _var_var_44, _var_var_39, _var_var_43, _var_var_42
                    )
                    _var_var_47 = _var_var_43.get(_var_var_39, {}).get(
                        "snow_covered", False
                    )
                    _var_var_48 = _var_var_42.get(_var_var_39, {}).get("shadow_type")
                    await self.db.update_prediction_panel_group_flags(
                        _var_var_6,
                        _var_var_39,
                        {
                            "exclude_from_learning_group": _var_var_45,
                            "exclusion_reason_group": (
                                _var_var_46 if _var_var_45 else None
                            ),
                            "snow_covered_group": _var_var_47,
                            "shadow_type_group": _var_var_48,
                        },
                    )
            _var_var_49 = await self._calculate_production_metrics(date)
            await self.db.save_hourly_production_metrics(_var_var_6, _var_var_49)
            _var_var_50 = await self._calculate_historical_context(date, hour)
            await self.db.save_hourly_historical_context(_var_var_6, _var_var_50)
            _LOGGER.debug(
                "Updated actual for %s: %.3f kWh, accuracy: %.1f%%",
                _var_var_6,
                actual_kwh,
                _var_var_17 or 0,
            )
            return True
        except Exception as _var_var_12:
            _LOGGER.error(
                "Failed to update hourly actual: %s", _var_var_12, exc_info=True
            )
            return False
