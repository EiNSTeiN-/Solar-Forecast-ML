# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from ..const import (
    DOMAIN,
    INTEGRATION_MODEL,
    AI_VERSION,
    SOFTWARE_VERSION,
    OPERATIONAL_REFORECAST_MODE_CUSTOM_TIME,
    OPERATIONAL_REFORECAST_MODE_STANDARD,
    OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY,
    OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY_AFTERNOON,
    DATA_KEY_FORECAST_TODAY,
    DATA_KEY_FORECAST_DAY_AFTER,
    DATA_KEY_PRODUCTION_TIME,
    DATA_KEY_PEAK_TODAY,
    DATA_KEY_EXPECTED_DAILY_PRODUCTION,
    DATA_KEY_STATISTICS,
    PROD_TIME_DURATION_SECONDS,
    PEAK_TODAY_POWER_W,
    STATS_ALL_TIME_PEAK,
    STATS_CURRENT_MONTH,
    STATS_CURRENT_WEEK,
    STATS_LAST_7_DAYS,
    STATS_LAST_30_DAYS,
    STATS_AVG_ACCURACY,
    STATS_YIELD_KWH,
    STATS_AVG_YIELD_KWH,
    STATS_CONSUMPTION_KWH,
    CACHE_HOURLY_PREDICTIONS,
    CACHE_PREDICTIONS,
    CACHE_PREDICTIONS_TOMORROW,
    CACHE_PREDICTIONS_DAY_AFTER,
    CACHE_BEST_HOUR_TODAY,
    PRED_TARGET_DATE,
    PRED_TARGET_HOUR,
    PRED_PREDICTION_KWH,
    PRED_PREDICTED_KWH,
)
from ..coordinator import SolarForecastMLCoordinator
from ..data.db_manager import DatabaseManager
from ..data.data_weather_radiation_ensemble import (
    MIN_PREDICTION_HOURS_TO_LOCK_DAY,
    hourly_forecast_date_covered,
)
from ..forecast.forecast_tfs_client import TFSClient
from .conservative_planning import (
    DEFAULT_SFML_WEIGHT,
    DEFAULT_TFS_P10_WEIGHT,
    cap_persisted_conservative_value,
    conservative_planning_value,
    p10_age_hours,
    p10_morning_recompute_allowed,
    parse_p10_timestamp,
    persisted_p10_freshness,
    remaining_conservative_value,
    remaining_today_expected_kwh,
    round_not_above,
)


async def _load_active_ops_day_forecast(
    db: Any, today_str: str
) -> Optional[dict[str, Any]]:
    """Return the active operational full-day forecast assembled from OPS hourly rows."""
    _var_var_5 = await db.fetchall(
        "SELECT target_hour, prediction_kwh, snapshot_type, snapshot_sequence,\n                  trigger_source, source_track, reforecast_cutoff_hour\n           FROM ops_hourly_forecasts\n           WHERE target_date = ?\n             AND is_active = TRUE\n           ORDER BY target_hour",
        (today_str,),
    )
    if not _var_var_5:
        return
    _var_var_6 = 0.0
    _var_var_7 = {}
    _var_var_8 = set()
    _var_var_9 = set()
    _var_var_10 = None
    _var_var_11 = None
    _var_var_12 = None
    for _var_var_13 in _var_var_5:
        _var_var_3 = int(_var_var_13[0])
        _var_var_14 = float(_var_var_13[1] or 0.0)
        _var_var_15 = str(_var_var_13[2]) if _var_var_13[2] is not None else None
        _var_var_16 = int(_var_var_13[3]) if _var_var_13[3] is not None else None
        _var_var_17 = str(_var_var_13[4]) if _var_var_13[4] is not None else None
        _var_var_18 = str(_var_var_13[5]) if _var_var_13[5] is not None else None
        _var_var_19 = int(_var_var_13[6]) if _var_var_13[6] is not None else None
        _var_var_7[f"{_var_var_3:02d}:00"] = round(_var_var_14, 3)
        _var_var_6 += _var_var_14
        if _var_var_15:
            _var_var_8.add(_var_var_15)
        if _var_var_18:
            _var_var_9.add(_var_var_18)
        if _var_var_16 is not None:
            if _var_var_10 is None or _var_var_16 > _var_var_10:
                _var_var_10 = _var_var_16
                _var_var_11 = _var_var_17
                _var_var_12 = _var_var_19
    _var_var_20 = await db.fetchall(
        "SELECT target_hour, group_name, prediction_kwh\n           FROM ops_prediction_panel_groups\n           WHERE target_date = ?\n             AND is_active = TRUE\n           ORDER BY target_hour, group_name",
        (today_str,),
    )
    _var_var_21 = {}
    _var_var_22 = {}
    for _var_var_13 in _var_var_20:
        _var_var_3 = int(_var_var_13[0])
        _var_var_23 = str(_var_var_13[1])
        _var_var_14 = round(float(_var_var_13[2] or 0.0), 4)
        _var_var_24 = f"{_var_var_3:02d}:00"
        if _var_var_24 not in _var_var_21:
            _var_var_21[_var_var_24] = {}
        _var_var_21[_var_var_24][_var_var_23] = _var_var_14
        _var_var_22[_var_var_23] = round(
            _var_var_22.get(_var_var_23, 0.0) + _var_var_14, 4
        )
    _var_var_25 = await db.fetchone(
        "SELECT prediction_kwh, snapshot_type\n           FROM ops_daily_forecasts\n           WHERE forecast_type = 'today'\n             AND forecast_date = ?\n             AND is_active = TRUE\n           ORDER BY snapshot_sequence DESC\n           LIMIT 1",
        (today_str,),
    )
    _var_var_26 = None
    _var_var_27 = None
    if _var_var_25:
        _var_var_26 = round(float(_var_var_25[0] or 0.0), 2)
        _var_var_15 = str(_var_var_25[1]) if _var_var_25[1] is not None else ""
        _var_var_27 = (
            "rest_of_day_total"
            if _var_var_15 in ("intraday_reforecast", "weather_event_reforecast")
            else "full_day_snapshot"
        )
    _var_var_28 = await db.fetchone(
        "SELECT prediction_kwh FROM daily_forecasts\n           WHERE forecast_type = 'today' AND forecast_date = ?\n           LIMIT 1",
        (today_str,),
    )
    _var_var_29 = round(float(_var_var_28[0] or 0.0), 2) if _var_var_28 else None
    _var_var_30 = await db.fetchall(
        "SELECT target_hour, actual_kwh FROM hourly_predictions\n           WHERE target_date = ? AND actual_kwh IS NOT NULL",
        (today_str,),
    )
    _var_var_31 = _var_var_12
    _var_var_32 = 0.0
    if _var_var_31 is not None:
        for _var_var_13 in _var_var_30:
            _var_var_3 = int(_var_var_13[0])
            if _var_var_3 <= int(_var_var_31):
                _var_var_32 += float(_var_var_13[1] or 0.0)
    _var_var_33 = None
    if (
        _var_var_27 == "rest_of_day_total"
        and _var_var_26 is not None
        and (_var_var_31 is not None)
    ):
        _var_var_33 = round(_var_var_32 + _var_var_26, 2)
    elif _var_var_29 is not None:
        _var_var_33 = _var_var_29
    return (
        (
            (
                {
                    "total_kwh": round(_var_var_6, 2),
                    "hourly_values": _var_var_7,
                    "hourly_group_values": _var_var_21,
                    "group_totals": _var_var_22,
                    "active_snapshot_types": sorted(_var_var_8),
                    "active_source_tracks": sorted(_var_var_9),
                    "latest_snapshot_sequence": _var_var_10,
                    "latest_trigger_source": _var_var_11,
                    "latest_cutoff_hour": _var_var_12,
                    "ops_today_row_kwh": _var_var_26,
                    "ops_today_row_scope": _var_var_27,
                    "morning_anchor_kwh": _var_var_29,
                    "actual_until_cutoff_kwh": round(_var_var_32, 2),
                    "expected_day_ist_plus_rest": _var_var_33,
                    "forecast_channel": (
                        "rest_of_day_ist_plus_sidecar"
                        if _var_var_27 == "rest_of_day_total"
                        and _var_var_33 is not None
                        else "morning_anchor"
                    ),
                }
                if True
                else None
            )
            if True
            else None
        )
        if True
        else None
    )


class ConservativePlanningForecastSensor(SensorEntity):

    async def _load_from_sources(self) -> None:
        """Load and blend conservative planning values from SFML cache and TFS. @zara"""
        self._cached_value = None
        self._hourly_values = {}
        self._hourly_group_values = {}
        self._group_totals = {}
        self._tfs_available = False
        self._forecast_locked = False
        self._forecast_lock_source = None
        self._forecast_locked_at = None
        self._normal_full_day_forecast = None
        self._remaining_conservative_forecast = None
        self._remaining_forecast_cap = None
        self._mapping_mode = "unknown"
        self._mapping_fallback_hours = []
        self._p10_value_updated_at = None
        self._p10_age_hours = None
        self._p10_source = "unavailable"
        self._p10_persisted_fresh = False
        self._p10_recomputed_at = None
        self._tfs_generated_at = None
        self._tfs_http_status = None
        self._fallback_reason = None
        try:
            from homeassistant.util import dt as _var_var_36

            _var_var_50 = _var_var_36.now().date().isoformat()
            _var_var_97 = await self._load_today_forecast_lock_state(_var_var_50)
            _var_var_87, _var_var_88 = await self._load_current_forecast_caps(
                _var_var_50
            )
            self._normal_full_day_forecast = _var_var_87
            self._remaining_forecast_cap = _var_var_88
            _var_var_98 = await self._load_persisted_value(
                _var_var_50, _var_var_87, _var_var_88
            )
            if _var_var_97 and _var_var_98 is not None and self._p10_persisted_fresh:
                self._cached_value = _var_var_98
                return
            if not _var_var_97:
                self._cached_value = _var_var_98
                return
            if not p10_morning_recompute_allowed(
                _var_var_50, self._forecast_lock_source, self._forecast_locked_at
            ):
                self._p10_source = "missing_morning_snapshot"
                if self._fallback_reason is None:
                    self._fallback_reason = "outside_morning_snapshot_window"
                self._log_p10_event_once(
                    logging.DEBUG,
                    (
                        "p10_recompute_blocked",
                        _var_var_50,
                        self._forecast_lock_source,
                        self._forecast_locked_at,
                    ),
                    "P10 planning value kept from the morning snapshot for %s (recomputed only inside the morning window; lock_source=%s, locked_at=%s, reason=%s)",
                    _var_var_50,
                    self._forecast_lock_source,
                    self._forecast_locked_at,
                    self._fallback_reason,
                )
                return
            _var_var_49 = await self._load_sfml_hourly_panel_groups(_var_var_50)
            if not _var_var_49:
                self._cached_value = _var_var_98
                return
            if self._tfs_client is None:
                self._tfs_client = TFSClient(self.hass)
            _var_var_99 = await self._fetch_tfs_quantiles()
            _var_var_100 = (
                self._parse_tfs_hourly_p10(_var_var_99) if _var_var_99 else {}
            )
            _var_var_101 = any(
                (
                    (_var_var_65.get("p10") or 0.0) > 0
                    or any(
                        (
                            (_var_var_79.get("p10") or 0.0) > 0
                            for _var_var_79 in _var_var_65.get("groups", [])
                        )
                    )
                    for _var_var_65 in _var_var_100.values()
                )
            )
            if _var_var_99 and (not _var_var_101):
                self._fallback_reason = "tfs_response_without_p10"
                _LOGGER.warning(
                    "ConservativePlanningForecastSensor: TFS response contains no p10 values for forecast_type=sfml"
                )
            elif not _var_var_99 and self._fallback_reason is None:
                self._fallback_reason = "tfs_unavailable"
            _var_var_102 = {}
            _var_var_21 = {}
            _var_var_22 = {}
            _var_var_103 = 0.0
            for _var_var_104 in _var_var_49:
                if _var_var_104.get(PRED_TARGET_DATE) != _var_var_50:
                    continue
                _var_var_3 = _var_var_104.get(PRED_TARGET_HOUR)
                _var_var_105 = _var_var_104.get("panel_group_predictions") or {}
                if _var_var_3 is None or not _var_var_105:
                    continue
                _var_var_107 = {
                    str(_var_var_23): float(_var_var_106 or 0.0)
                    for _var_var_23, _var_var_106 in _var_var_105.items()
                }
                _var_var_24 = f"{int(_var_var_3):02d}:00"
                _var_var_76 = f"{_var_var_50}T{int(_var_var_3):02d}:00:00"
                _var_var_108 = _var_var_100.get(_var_var_76)
                _var_var_109 = _var_var_108.get("groups") if _var_var_108 else None
                _var_var_110 = {}
                if _var_var_109:
                    _var_var_110 = self._map_tfs_groups(
                        _var_var_109, _var_var_107, _var_var_24
                    )
                    if not _var_var_110:
                        self._mapping_mode = "sfml_only_fallback"
                        self._mapping_fallback_hours.append(_var_var_24)
                elif self._mapping_mode == "unknown":
                    self._mapping_mode = "sfml_only_fallback"
                _var_var_111 = {}
                for _var_var_23, _var_var_112 in _var_var_107.items():
                    _var_var_113 = conservative_planning_value(
                        _var_var_112,
                        _var_var_110.get(_var_var_23),
                        sfml_weight=self._sfml_weight,
                        tfs_weight=self._tfs_weight,
                        precision=4,
                    )
                    _var_var_111[_var_var_23] = _var_var_113
                    _var_var_22[_var_var_23] = round(
                        _var_var_22.get(_var_var_23, 0.0) + _var_var_113, 4
                    )
                if not _var_var_111:
                    continue
                _var_var_114 = sum(
                    (max(0.0, _var_var_53) for _var_var_53 in _var_var_107.values())
                )
                _var_var_103 += _var_var_114
                _var_var_115 = round_not_above(
                    sum(_var_var_111.values()), _var_var_114, 4
                )
                _var_var_102[_var_var_24] = _var_var_115
                _var_var_21[_var_var_24] = _var_var_111
            if not _var_var_102:
                return
            self._tfs_available = _var_var_101
            self._p10_source = (
                "morning_snapshot_tfs"
                if _var_var_101
                else "morning_snapshot_sfml_fallback"
            )
            if _var_var_101:
                self._fallback_reason = None
            if self._mapping_mode == "unknown":
                self._mapping_mode = "name" if _var_var_101 else "sfml_only_fallback"
            _var_var_116 = round(_var_var_103, 2)
            self._normal_full_day_forecast = (
                min(self._normal_full_day_forecast, _var_var_116)
                if self._normal_full_day_forecast is not None
                else _var_var_116
            )
            self._cached_value = round_not_above(
                sum(_var_var_102.values()), _var_var_116, 2
            )
            self._remaining_conservative_forecast = remaining_conservative_value(
                self._cached_value,
                self._today_actual_total(_var_var_50),
                self._remaining_forecast_cap,
            )
            self._hourly_values = {
                _var_var_24: round(_var_var_53, 3)
                for _var_var_24, _var_var_53 in _var_var_102.items()
            }
            self._hourly_group_values = {
                _var_var_24: {
                    _var_var_23: round(_var_var_53, 4)
                    for _var_var_23, _var_var_53 in _var_var_117.items()
                }
                for _var_var_24, _var_var_117 in _var_var_21.items()
            }
            self._group_totals = {
                _var_var_23: round(_var_var_53, 3)
                for _var_var_23, _var_var_53 in _var_var_22.items()
            }
            await self._persist_final_value(_var_var_50, self._cached_value)
            self._log_p10_event_once(
                logging.INFO,
                ("recomputed_p10", _var_var_50, self._cached_value, self._p10_source),
                "ConservativePlanningForecastSensor: recomputed P10 value=%.2f date=%s source=%s tfs_generated_at=%s fallback_reason=%s",
                self._cached_value,
                _var_var_50,
                self._p10_source,
                self._tfs_generated_at,
                self._fallback_reason,
            )
        except Exception as _var_var_46:
            _LOGGER.warning(
                "Failed to load ConservativePlanningForecastSensor: %s", _var_var_46
            )
            try:
                from homeassistant.util import dt as _var_var_36

                _var_var_118 = _var_var_36.now().date().isoformat()
                await self._load_today_forecast_lock_state(_var_var_118)
                self._cached_value = await self._load_persisted_value(_var_var_118)
            except Exception:
                self._cached_value = None
            self._hourly_values = {}
            self._hourly_group_values = {}
            self._group_totals = {}
            self._tfs_available = False
        if True:
            return None
        else:
            return None
