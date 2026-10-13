# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import calendar
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
import aiosqlite
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from . import context
from .common import (
    PRODUCTION_CANDIDATE_KWH_THRESHOLD,
    _build_forecast_error_metrics,
    _build_live_today_clean_eval,
    _clock_time,
    _column_expr,
    _corrected_daily_energy_values,
    _energy_correction_projection,
    _get_config,
    _get_db,
    _get_fixed_total_price,
    _get_local_naive_range_for_local_date,
    _get_power_w,
    _get_sensor_value,
    _get_solar_reader,
    _get_stats_gpm_coordinator,
    _gpm_local_hour_key,
    _panel_group_exclusion_reason,
    _stats_visible_exclusion_reason,
    _table_columns,
    _time_context,
    local_only,
)
from .hubble import (
    _hubble_best_window,
    _hubble_build_memory,
    _hubble_decline_hour,
    _hubble_has_battery,
    _hubble_read_workflow_status,
    _hubble_recommendation_start_hour,
    _hubble_resolve_moment,
    _hubble_round,
    _hubble_time_to_minutes,
)
from ..core.shadow_context import build_shadow_signal
from ..core.heatpump_today import capped_hp_pv_kwh, hp_pv_share_percent
from ..core.grid_split import SPLIT_TOLERANCE_W, reconcile_grid_split
from ..core.corrected_energy import (
    _resolve_price,
    component_day,
    corrected_month_sum,
    corrected_value,
    dynamic_month_costs,
    energy_flows_changed,
    dynamic_month_average,
    legacy_dynamic_month,
    load_hourly_component_days,
    monthly_table_energy_cost,
    window_autarky_average,
    yesterday_figures,
)
from ..utils.time_utils import as_ha_local, ha_now, ha_today
from ..core.price_mode import (
    MODE_LEGACY_FIXED,
    effective_price_mode,
    feed_in_tariff_ct,
    is_fixed_like,
)
from ..const import (
    DOMAIN,
    CONF_SENSOR_SOLAR_TO_HOUSE,
    CONF_SENSOR_INVERTER_AC_OUTPUT,
    CONF_SENSOR_SOLAR_TO_BATTERY,
    CONF_SENSOR_BATTERY_TO_HOUSE,
    CONF_SENSOR_GRID_TO_HOUSE,
    CONF_SENSOR_GRID_TO_BATTERY,
    CONF_SENSOR_HOUSE_TO_GRID,
    CONF_SENSOR_SMARTMETER_IMPORT,
    CONF_SENSOR_SMARTMETER_EXPORT,
    CONF_SENSOR_BATTERY_SOC,
    CONF_SENSOR_BATTERY_POWER,
    CONF_SENSOR_HOME_CONSUMPTION,
    CONF_WEATHER_ENTITY,
    CONF_FEED_IN_TARIFF,
    DEFAULT_FEED_IN_TARIFF,
    CONF_BILLING_START_DAY,
    CONF_BILLING_START_MONTH,
    CONF_BILLING_BASE_FEE,
    DEFAULT_BILLING_START_DAY,
    DEFAULT_BILLING_START_MONTH,
    DEFAULT_BILLING_WORK_PRICE,
    DEFAULT_BILLING_GRID_FEES,
    CONF_PANEL_GROUP_NAMES,
    CONF_SHOW_PANEL_GROUPS,
    CONF_SENSOR_HEATPUMP_POWER,
    CONF_SENSOR_HEATPUMP_COP,
    CONF_SENSOR_HEATINGROD_POWER,
    CONF_SENSOR_WALLBOX_POWER,
    CONF_SENSOR_WALLBOX_STATE,
    DEFAULT_HEATPUMP_COP,
)
from ..readers.solar_reader import PanelGroupData
from ..readers.annual_forecast_reader import AnnualForecastReader
from ..readers.weather_reader import WeatherDataReader
from ..sfml_data_reader import SFMLDataReader
from ..core.consumer_atlas import calculate_billing_period
from ..core.energy_balance import build_daily_supply_balance
from ..core.energy_efficiency import build_energy_chain_summary


async def _corrected_finalized_solar_values(start_date: str) -> dict[str, float]:
    """Project PV yield for the exact finalized-day population used by annual reports."""
    _var_var_168 = await _energy_correction_projection(start_date)
    _var_var_169 = {}
    async with _get_db() as _var_var_32:
        async with _var_var_32.execute(
            "SELECT date, actual_total_kwh solar_yield_kwh\n               FROM daily_summaries\n               WHERE date >= ? AND actual_total_kwh > 0",
            (start_date,),
        ) as _var_var_33:
            for _var_var_30 in await _var_var_33.fetchall():
                _var_var_169[_var_var_30["date"]] = corrected_value(
                    _var_var_168,
                    _var_var_30["date"],
                    "solar_yield_day_kwh",
                    _var_var_30["solar_yield_kwh"],
                )
    if True:
        return _var_var_169


def _resolve_entity_id_by_unique_suffix(
    entity_domain: str,
    platform: str,
    suffixes: tuple[str, ...],
    candidates: tuple[str, ...] = (),
) -> str | None:
    if context.HASS is None:
        return
    for _var_var_344 in candidates:
        _var_var_38 = context.HASS.states.get(_var_var_344)
        if _var_var_38 is None:
            continue
        if not _var_var_38.state not in ("unknown", "unavailable", None):
            continue
        return _var_var_344
    try:
        _var_var_345 = er.async_get(context.HASS)
        for _var_var_346 in _var_var_345.entities.values():
            if (
                _var_var_346.domain != entity_domain
                or _var_var_346.platform != platform
            ):
                continue
            A_recovered_capture_0001 = str(_var_var_346.unique_id or "").lower()
            if not any(
                (
                    A_recovered_capture_0001.endswith(_var_var_349.lower())
                    for _var_var_349 in suffixes
                )
            ):
                continue
            if _var_var_346.disabled_by is not None:
                continue
            _var_var_38 = context.HASS.states.get(_var_var_346.entity_id)
            if _var_var_38 is not None:
                if _var_var_38.state not in ("unknown", "unavailable", None):
                    return _var_var_346.entity_id
    except Exception as _var_var_5:
        _LOGGER.debug("Entity registry status-chip lookup failed: %s", _var_var_5)
    _var_var_347 = tuple((_var_var_349.lower() for _var_var_349 in suffixes))
    for _var_var_38 in context.HASS.states.async_all(entity_domain):
        _var_var_348 = _var_var_38.entity_id.split(".", 1)[-1].lower()
        if not _var_var_348.endswith(_var_var_347):
            continue
        if not _var_var_38.state not in ("unknown", "unavailable", None):
            continue
        return _var_var_38.entity_id


class StatisticsView(HomeAssistantView):

    async def _get_panel_group_data(
        self, target_date: date | None = None
    ) -> dict[str, Any]:
        """Extract panel group predictions and actuals for a local day. @zara"""
        _var_var_50 = _get_config()
        if _var_var_50.get(CONF_SHOW_PANEL_GROUPS) is False:
            return {"available": False, "groups": {}}
        _var_var_384 = target_date or ha_today(context.HASS)
        _var_var_394 = _var_var_384 == ha_today(context.HASS)
        _var_var_330 = ha_now(context.HASS).hour if _var_var_394 else 23
        _var_var_395 = {}
        if _var_var_394:
            try:
                for _var_var_396 in await SFMLDataReader(
                    context.HASS
                ).get_live_panel_groups():
                    _var_var_395[normalized_group_key(_var_var_396.get("name"))] = (
                        _var_var_396
                    )
            except Exception as _var_var_171:
                _LOGGER.debug("Could not load live panel group SOT: %s", _var_var_171)
        try:
            _var_var_84 = _get_solar_reader()
            _var_var_397 = await _var_var_84.async_get_panel_group_data(
                target_date=_var_var_384
            )
            if not _var_var_397:
                _var_var_397 = await self._get_panel_group_forecast_fallback(
                    _var_var_384
                )
                if not _var_var_397 and (not _var_var_395):
                    return {
                        "available": False,
                        "target_date": _var_var_384.isoformat(),
                        "groups": {},
                    }
                if not _var_var_397:
                    return {
                        "available": True,
                        "target_date": _var_var_384.isoformat(),
                        "forecast_available": False,
                        "live_only": True,
                        "groups": self._build_live_panel_group_fallback(_var_var_395),
                    }
        except Exception as _var_var_171:
            _LOGGER.error(
                "Error loading panel group data from database: %s", _var_var_171
            )
            _var_var_397 = await self._get_panel_group_forecast_fallback(_var_var_384)
            if not _var_var_397 and (not _var_var_395):
                return {
                    "available": False,
                    "target_date": _var_var_384.isoformat(),
                    "groups": {},
                }
            if not _var_var_397:
                return {
                    "available": True,
                    "target_date": _var_var_384.isoformat(),
                    "forecast_available": False,
                    "live_only": True,
                    "groups": self._build_live_panel_group_fallback(_var_var_395),
                }
        _var_var_50 = _get_config()
        _var_var_398 = _var_var_50.get(CONF_PANEL_GROUP_NAMES, {})
        if not isinstance(_var_var_398, dict):
            _var_var_398 = {}
        _var_var_399 = {}
        for _var_var_400, _var_var_207 in _var_var_397.items():
            _var_var_401 = _var_var_398.get(_var_var_400, _var_var_400)
            _var_var_396 = _var_var_395.get(
                normalized_group_key(_var_var_400)
            ) or _var_var_395.get(normalized_group_key(_var_var_401))
            _var_var_402 = None
            if _var_var_396 and _var_var_396.get("actual_today_kwh") is not None:
                try:
                    _var_var_402 = max(
                        0.0, float(_var_var_396.get("actual_today_kwh") or 0.0)
                    )
                except (TypeError, ValueError):
                    _var_var_402 = None
            _var_var_403 = {
                "name": _var_var_401,
                "original_name": _var_var_400,
                "prediction_day_kwh": 0.0,
                "prediction_until_now_kwh": 0.0,
                "actual_until_now_kwh": 0.0,
                "clean_prediction_until_now_kwh": 0.0,
                "clean_actual_until_now_kwh": 0.0,
                "raw_prediction_until_now_kwh": 0.0,
                "raw_actual_until_now_kwh": 0.0,
                "day_progress_percent": None,
                "forecast_until_now_accuracy_percent": None,
                "clean_accuracy_percent": None,
                "elapsed_hours_count": 0,
                "clean_hours_count": 0,
                "excluded_hours_count": 0,
                "missing_actual_hours_count": 0,
                "exclusion_reason_breakdown": {},
                "hourly": [],
                "forecast_available": True,
                "live_only": False,
            }
            _var_var_404 = 0.0
            _var_var_405 = None
            for _var_var_406 in _var_var_207:
                _var_var_407 = _var_var_406.prediction_kwh
                _var_var_17 = _var_var_406.actual_kwh
                _var_var_194 = (
                    _var_var_406.target_hour
                    if _var_var_406.target_hour is not None
                    else 0
                )
                _var_var_408 = _panel_group_exclusion_reason(_var_var_406)
                _var_var_409 = _var_var_408 is not None
                if _var_var_407 is not None:
                    _var_var_403["prediction_day_kwh"] += _var_var_407
                if _var_var_17 is not None:
                    _var_var_404 += _var_var_17
                if _var_var_194 <= _var_var_330:
                    _var_var_403["elapsed_hours_count"] += 1
                    if _var_var_407 is not None:
                        _var_var_403["prediction_until_now_kwh"] += _var_var_407
                        _var_var_403["raw_prediction_until_now_kwh"] += _var_var_407
                    if _var_var_17 is not None:
                        _var_var_403["actual_until_now_kwh"] += _var_var_17
                        _var_var_403["raw_actual_until_now_kwh"] += _var_var_17
                    else:
                        _var_var_403["missing_actual_hours_count"] += 1
                    if _var_var_409:
                        _var_var_403["excluded_hours_count"] += 1
                        _var_var_158 = _var_var_403["exclusion_reason_breakdown"]
                        _var_var_158[_var_var_408] = (
                            _var_var_158.get(_var_var_408, 0) + 1
                        )
                    elif _var_var_17 is not None:
                        _var_var_403["clean_hours_count"] += 1
                        if _var_var_407 is not None:
                            _var_var_403[
                                "clean_prediction_until_now_kwh"
                            ] += _var_var_407
                        _var_var_403["clean_actual_until_now_kwh"] += _var_var_17
                _var_var_410 = {
                    "hour": _var_var_194,
                    "prediction_kwh": _var_var_407,
                    "actual_kwh": _var_var_17,
                    "excluded": _var_var_409,
                    "exclusion_reason": _var_var_408,
                }
                _var_var_403["hourly"].append(_var_var_410)
                if _var_var_194 == _var_var_330:
                    _var_var_405 = _var_var_410
            _var_var_403["actual_source"] = "db_hourly_sum"
            _var_var_403["prediction_total_kwh"] = _var_var_403["prediction_day_kwh"]
            if _var_var_394 and _var_var_402 is not None:
                _var_var_411 = _var_var_402 - _var_var_404
                if _var_var_411 > 0.01 and _var_var_403["hourly"]:
                    _var_var_412 = _var_var_405
                    if _var_var_412 is None:
                        _var_var_413 = [
                            _var_var_30
                            for _var_var_30 in _var_var_403["hourly"]
                            if int(_var_var_30.get("hour") or -1) <= _var_var_330
                        ]
                        _var_var_412 = (
                            _var_var_413[-1]
                            if _var_var_413
                            else _var_var_403["hourly"][-1]
                        )
                    _var_var_412["actual_kwh"] = (
                        _var_var_412.get("actual_kwh") or 0.0
                    ) + _var_var_411
                    if int(_var_var_412.get("hour") or -1) <= _var_var_330:
                        _var_var_403["raw_actual_until_now_kwh"] += _var_var_411
                        if not _var_var_412.get("excluded"):
                            if (_var_var_412.get("actual_kwh") or 0.0) == _var_var_411:
                                _var_var_403["clean_hours_count"] += 1
                                _var_var_403["missing_actual_hours_count"] = max(
                                    0, _var_var_403["missing_actual_hours_count"] - 1
                                )
                                _var_var_403["clean_prediction_until_now_kwh"] += (
                                    _var_var_412.get("prediction_kwh") or 0.0
                                )
                            _var_var_403["clean_actual_until_now_kwh"] += _var_var_411
                _var_var_403["actual_until_now_kwh"] = _var_var_402
                _var_var_403["actual_total_kwh"] = _var_var_402
                _var_var_403["raw_actual_until_now_kwh"] = _var_var_402
                _var_var_403["actual_source"] = "actual_live_panel_groups"
                _var_var_403["live_power_w"] = (
                    _var_var_396.get("power") if _var_var_396 else None
                )
                _var_var_403["live_quality"] = (
                    _var_var_396.get("quality") if _var_var_396 else None
                )
                _var_var_403["live_updated_at"] = (
                    _var_var_396.get("updated_at") if _var_var_396 else None
                )
            else:
                _var_var_403["actual_total_kwh"] = _var_var_403["actual_until_now_kwh"]
            if _var_var_403["prediction_day_kwh"] > 0:
                _var_var_403["day_progress_percent"] = (
                    _var_var_403["actual_total_kwh"]
                    / _var_var_403["prediction_day_kwh"]
                    * 100
                )
            if (
                _var_var_403["raw_prediction_until_now_kwh"] > 0
                and _var_var_403["raw_actual_until_now_kwh"] > 0
            ):
                _var_var_414 = (
                    abs(
                        (
                            _var_var_403["raw_actual_until_now_kwh"]
                            - _var_var_403["raw_prediction_until_now_kwh"]
                        )
                        / _var_var_403["raw_prediction_until_now_kwh"]
                    )
                    * 100
                )
                _var_var_403["raw_accuracy_percent"] = max(
                    0, min(100, 100 - _var_var_414)
                )
                _var_var_403["forecast_until_now_accuracy_percent"] = _var_var_403[
                    "raw_accuracy_percent"
                ]
            else:
                _var_var_403["raw_accuracy_percent"] = None
            if (
                _var_var_403["clean_prediction_until_now_kwh"] > 0
                and _var_var_403["clean_actual_until_now_kwh"] > 0
            ):
                _var_var_415 = (
                    abs(
                        (
                            _var_var_403["clean_actual_until_now_kwh"]
                            - _var_var_403["clean_prediction_until_now_kwh"]
                        )
                        / _var_var_403["clean_prediction_until_now_kwh"]
                    )
                    * 100
                )
                _var_var_403["accuracy_percent"] = max(0, min(100, 100 - _var_var_415))
                _var_var_403["clean_accuracy_percent"] = _var_var_403[
                    "accuracy_percent"
                ]
            else:
                _var_var_403["accuracy_percent"] = None
            _var_var_399[_var_var_401] = _var_var_403
            continue
        return {
            "available": True,
            "target_date": _var_var_384.isoformat(),
            "forecast_available": True,
            "live_only": False,
            "groups": _var_var_399,
        }
