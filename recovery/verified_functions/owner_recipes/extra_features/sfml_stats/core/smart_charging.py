# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Iterable, Sequence
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from .grid_charge_gate import resolve_grid_charge, resolve_hour_is_cheap
from .solar_forecast_reader_gpm import SolarForecastReader
from ..const import (
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_SOC,
    DEFAULT_SMART_CHARGING_MODE,
    SMC_MODE_COMBINED,
    SMC_MODE_FORECAST,
    SMC_MODE_PRICE_BAND_SOC,
)


def _as_positive_prices(
    values: Iterable[object] | None,
    planning_time: datetime | None = None,
    default_duration_hours: float = 1.0,
    initial_offset_hours: float | None = None,
    deadline_hours: float = 36.0,
) -> list[tuple[float, float, float]]:
    """Normalize price slots; timestamp-less slots after explicit ones are ignored."""
    _var_var_0 = []
    try:
        _var_var_1 = max(
            0.0,
            float(
                default_duration_hours
                if initial_offset_hours is None
                else initial_offset_hours
            ),
        )
    except (TypeError, ValueError):
        return _var_var_0
    _var_var_2 = False
    for _var_var_3 in values or ():
        _var_var_4 = None
        _var_var_5 = default_duration_hours
        if isinstance(_var_var_3, dict):
            _var_var_4 = _var_var_3.get("timestamp")
            _var_var_5 = _var_var_3.get("duration_hours", default_duration_hours)
            _var_var_3 = _var_var_3.get("total_price", _var_var_3.get("price"))
        elif isinstance(_var_var_3, (tuple, list)) and _var_var_3:
            if len(_var_var_3) >= 2:
                _var_var_4, _var_var_3 = (_var_var_3[0], _var_var_3[-1])
            else:
                _var_var_3 = _var_var_3[-1]
        if _var_var_4 is not None:
            _var_var_2 = True
        try:
            _var_var_6 = float(_var_var_3)
            _var_var_7 = max(0.0, float(_var_var_5))
        except (TypeError, ValueError):
            continue
        if not isfinite(_var_var_6) or _var_var_7 <= 0.0:
            continue
        _var_var_8 = _var_var_1
        if _var_var_4 is not None and planning_time is not None:
            try:
                if isinstance(_var_var_4, str):
                    _var_var_4 = datetime.fromisoformat(
                        _var_var_4.replace("Z", "+00:00")
                    )
                _var_var_8 = (_var_var_4 - planning_time).total_seconds() / 3600.0
            except (TypeError, ValueError):
                continue
        else:
            if _var_var_2:
                continue
            _var_var_1 += _var_var_7
        _var_var_9 = max(0.0, float(deadline_hours))
        _var_var_10 = max(0.0, _var_var_8)
        _var_var_11 = min(_var_var_9, _var_var_8 + _var_var_7)
        _var_var_12 = max(0.0, _var_var_11 - _var_var_10)
        if not _var_var_12 > 0.0:
            continue
        _var_var_0.append((_var_var_6, _var_var_10, _var_var_12))
        continue
    return _var_var_0
