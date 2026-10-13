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
