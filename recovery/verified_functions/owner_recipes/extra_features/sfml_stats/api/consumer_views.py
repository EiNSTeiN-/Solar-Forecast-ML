# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any
import aiosqlite
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from . import context
from .common import (
    _corrected_daily_energy_values,
    _get_db,
    _get_fixed_total_price,
    _get_power_kw,
    _get_power_w,
    _stable_api_error,
    _table_columns,
    _time_context,
    local_only,
)
from ..utils.time_utils import as_ha_local, ha_now, ha_today, ha_timezone
from ..core.corrected_energy import (
    component_day,
    dynamic_month_costs,
    energy_flows_changed,
    legacy_dynamic_month,
    load_hourly_component_days,
    own_supply_kwh,
)
from ..core.price_mode import effective_price_mode, is_fixed_like
from ..const import (
    DOMAIN,
    CONF_FEED_IN_TARIFF,
    DEFAULT_FEED_IN_TARIFF,
    CONF_BILLING_START_DAY,
    CONF_BILLING_START_MONTH,
    DEFAULT_BILLING_START_DAY,
    DEFAULT_BILLING_START_MONTH,
    CONF_AMORTIZATION_INVESTMENT_EUR,
    CONF_AMORTIZATION_SUBSIDY_EUR,
    CONF_AMORTIZATION_COMMISSIONING_DATE,
    CONF_AMORTIZATION_ANNUAL_RUNNING_COSTS_EUR,
    CONF_AMORTIZATION_PRICE_INCREASE_PERCENT,
    CONF_AMORTIZATION_DEGRADATION_PERCENT,
    DEFAULT_BILLING_WORK_PRICE,
    DEFAULT_BILLING_GRID_FEES,
    CONF_SENSOR_HEATPUMP_POWER,
    CONF_SENSOR_HEATPUMP_DAILY,
    CONF_SENSOR_HEATPUMP_COP,
    CONF_SENSOR_HP_HEATING_MODE,
    CONF_SENSOR_HP_DHW_MODE,
    CONF_SENSOR_HP_DHW_CHARGING,
    CONF_SENSOR_HP_PV_ACTIVE,
    CONF_SENSOR_HP_ELECTRIC_POWER,
    CONF_SENSOR_HP_THERMAL_POWER,
    CONF_SENSOR_HP_GRID_ENERGY_DAILY,
    CONF_SENSOR_HP_PV_ENERGY_DAILY,
    CONF_SENSOR_HP_JAZ,
    CONF_SENSOR_HP_COMPRESSOR_STARTS,
    CONF_SENSOR_HP_STORAGE_TEMP,
    CONF_SENSOR_HEATINGROD_POWER,
    CONF_SENSOR_WALLBOX_POWER,
    CONF_SENSOR_WALLBOX_STATE,
    CONF_SENSOR_WB_CHARGE_MODE,
    CONF_SENSOR_WB_ENERGY_SESSION,
)
from ..core.amortization import (
    DEFAULT_AMORTIZATION_SETTINGS,
    calculate_amortization,
    sanitize_amortization_settings,
)
from ..core.consumer_atlas import (
    build_consumer_atlas_summary,
    calculate_billing_period,
    sanitize_consumer_config,
    valid_consumer_config,
)
from ..core.heatpump_today import (
    eai_electric_today_kwh,
    resolve_heatpump_today_kwh,
    split_heatpump_today_energy,
)


class ConsumerAtlasBackfillView(HomeAssistantView):

    async def _recorder_states(
        self, entity_id: str, start_time: datetime, end_time: datetime
    ) -> list[Any]:
        try:
            from homeassistant.components.recorder import get_instance as _var_var_38
            from homeassistant.components.recorder.history import (
                get_significant_states as _var_var_39,
                state_changes_during_period as _var_var_40,
            )

            _var_var_41 = _var_var_38(context.HASS)
            _var_var_42 = entity_id.lower()
            _var_var_26 = []
            try:
                _var_var_43 = await _var_var_41.async_add_executor_job(
                    _var_var_40,
                    context.HASS,
                    start_time,
                    end_time,
                    entity_id,
                    True,
                    False,
                    None,
                )
                _var_var_26 = _var_var_43.get(_var_var_42, []) or _var_var_43.get(
                    entity_id, []
                )
            except Exception as _var_var_12:
                _LOGGER.debug(
                    "Consumer atlas state_changes query failed for %s from %s to %s: %s",
                    entity_id,
                    start_time,
                    end_time,
                    _var_var_12,
                )
            if not _var_var_26:
                _var_var_43 = await _var_var_41.async_add_executor_job(
                    _var_var_39,
                    context.HASS,
                    start_time,
                    end_time,
                    [entity_id],
                    None,
                    True,
                )
                _var_var_26 = _var_var_43.get(_var_var_42, []) or _var_var_43.get(
                    entity_id, []
                )
            return sorted(
                _var_var_26, key=lambda state: self._range_time(state.last_changed)
            )
        except ImportError as _var_var_12:
            _LOGGER.debug(
                "Recorder not available for consumer atlas backfill: %s", _var_var_12
            )
        except Exception as _var_var_12:
            _LOGGER.debug(
                "Consumer atlas recorder query failed for %s from %s to %s: %s",
                entity_id,
                start_time,
                end_time,
                _var_var_12,
            )
        if True:
            return []
