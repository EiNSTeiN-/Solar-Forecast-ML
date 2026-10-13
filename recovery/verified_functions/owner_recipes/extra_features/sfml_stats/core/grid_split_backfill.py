# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import asyncio
import logging
from typing import Any, Awaitable, Callable
from .grid_split import (
    META_GRID_SPLIT_BACKFILL,
    META_GRID_SPLIT_DAILY_BACKFILL,
    META_GRID_SPLIT_DONE,
    energy_cost_ct,
    reconcile_grid_split,
    split_tolerance_kwh,
)
from .price_mode import MODE_NONE, _present_price, resolve_hour_price


async def async_backfill_grid_split(
    hass: Any,
    *,
    config: dict[str, Any] | None = None,
    manager: Any | None = None,
    batch_size: int = BATCH_ROWS,
    pause_s: float = BATCH_PAUSE_S,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    hour_key_now: str | None = None,
    mode: str | None = None,
    fixed_total_ct: float | None = None,
) -> int:
    """Repair closed hours. Returns the number of rows actually updated."""
    config = config or {}
    from .price_mode import (
        effective_price_mode as _var_var_13,
        gpm_fixed_total_ct as _var_var_14,
    )

    if mode is None:
        mode = _var_var_13(hass, config)
    if fixed_total_ct is None and mode != MODE_NONE:
        fixed_total_ct = _var_var_14(hass)
    if manager is None:
        from ..storage.db_connection_manager import (
            DatabaseConnectionManager as _var_var_15,
            get_manager as _var_var_16,
        )

        manager = _var_var_16()
        if manager is None:
            try:
                manager = await _var_var_15.get_instance(hass)
            except Exception as _var_var_17:
                _LOGGER.debug(
                    "Grid-split backfill skipped, no database manager: %s", _var_var_17
                )
                return 0
    if hour_key_now is None:
        from ..utils.time_utils import (
            format_local_hour_key as _var_var_18,
            ha_now as _var_var_19,
            ha_today as _var_var_20,
        )

        _var_var_21 = _var_var_19(hass)
        hour_key_now = _var_var_18(_var_var_21, hass)
        _var_var_22 = _var_var_20(hass).isoformat()
    else:
        _var_var_22 = str(hour_key_now)[:10]
    _var_var_23 = fixed_total_ct
    _var_var_24 = sleep or asyncio.sleep
    _var_var_25 = 0
    while True:
        async with manager.write_transaction() as _var_var_26:
            _var_var_27 = await _meta_get(_var_var_26, META_GRID_SPLIT_BACKFILL)
            if _var_var_27 == META_GRID_SPLIT_DONE:
                return _var_var_25
            _var_var_28 = _var_var_27 or ""
            _var_var_29 = await _load_averages(_var_var_26)
            _var_var_30 = await _current_average(_var_var_26)
            try:
                async with _var_var_26.execute(
                    _ROW_SQL, (_var_var_28, hour_key_now, int(batch_size))
                ) as _var_var_0:
                    _var_var_31 = await _var_var_0.fetchall()
            except Exception as _var_var_17:
                _LOGGER.warning("Grid-split backfill read failed: %s", _var_var_17)
                return _var_var_25
            if not _var_var_31:
                await _meta_set(
                    _var_var_26, META_GRID_SPLIT_BACKFILL, META_GRID_SPLIT_DONE
                )
                _LOGGER.info(
                    "Grid-split backfill finished: %s rows updated", _var_var_25
                )
                return _var_var_25
            for _var_var_1 in _var_var_31:
                _var_var_32 = _price_for_row(
                    _var_var_1,
                    mode=mode,
                    config=config,
                    fixed_total_ct=_var_var_23,
                    daily_averages=_var_var_29,
                    current_average=_var_var_30,
                    today_str=_var_var_22,
                )
                if await _apply_row(_var_var_26, _var_var_1, _var_var_32):
                    _var_var_25 += 1
            _var_var_33 = str(_col(_var_var_31[-1], "hour_key", 0))
            await _meta_set(_var_var_26, META_GRID_SPLIT_BACKFILL, _var_var_33)
        await _var_var_24(pause_s)


async def async_backfill_daily_grid_split(
    hass: Any,
    *,
    manager: Any | None = None,
    batch_size: int = BATCH_ROWS,
    pause_s: float = BATCH_PAUSE_S,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    today_str: str | None = None,
) -> int:
    """Repair closed days that stored an independent import meter.

    Returns the number of rows actually updated. Same batch and pause as
    the hourly backfill, with its own marker.
    """
    if manager is None:
        from ..storage.db_connection_manager import (
            DatabaseConnectionManager as _var_var_15,
            get_manager as _var_var_16,
        )

        manager = _var_var_16()
        if manager is None:
            try:
                manager = await _var_var_15.get_instance(hass)
            except Exception as _var_var_17:
                _LOGGER.debug(
                    "Daily grid-split backfill skipped, no database manager: %s",
                    _var_var_17,
                )
                return 0
    if today_str is None:
        from ..utils.time_utils import ha_today as _var_var_20

        today_str = _var_var_20(hass).isoformat()
    _var_var_24 = sleep or asyncio.sleep
    _var_var_25 = 0
    while True:
        async with manager.write_transaction() as _var_var_26:
            _var_var_27 = await _meta_get(_var_var_26, META_GRID_SPLIT_DAILY_BACKFILL)
            if _var_var_27 == META_GRID_SPLIT_DONE:
                return _var_var_25
            _var_var_28 = _var_var_27 or ""
            try:
                async with _var_var_26.execute(
                    _DAILY_SQL, (_var_var_28, today_str, int(batch_size))
                ) as _var_var_0:
                    _var_var_31 = await _var_var_0.fetchall()
            except Exception as _var_var_17:
                _LOGGER.warning(
                    "Daily grid-split backfill read failed: %s", _var_var_17
                )
                return _var_var_25
            if not _var_var_31:
                await _meta_set(
                    _var_var_26, META_GRID_SPLIT_DAILY_BACKFILL, META_GRID_SPLIT_DONE
                )
                _LOGGER.info(
                    "Daily grid-split backfill finished: %s rows updated", _var_var_25
                )
                return _var_var_25
            for _var_var_1 in _var_var_31:
                if await _apply_daily_row(_var_var_26, _var_var_1):
                    _var_var_25 += 1
            _var_var_33 = str(_col(_var_var_31[-1], "date", 0))
            await _meta_set(_var_var_26, META_GRID_SPLIT_DAILY_BACKFILL, _var_var_33)
        await _var_var_24(pause_s)
