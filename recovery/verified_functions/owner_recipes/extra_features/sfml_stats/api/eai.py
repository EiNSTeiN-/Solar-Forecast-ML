# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import json
from itertools import chain, islice
import logging
from math import isfinite
import re
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..core.eai_wp_sources import heatpump_setup_status
from ..mock.eai import SCENARIOS, scenario_payload
from .premium_dashboard import _eai_config, _eai_feature_enabled
from .views import local_only


def _public_mobility_data(data: dict[str, Any]) -> dict[str, Any]:
    """Expose only the current, enumerated Mobility DTO."""
    _var_var_3 = {
        _var_var_0: _var_var_2
        for _var_var_0, _var_var_2 in data.items()
        if _var_var_0 in _MOBILITY_PUBLIC_KEYS
    }
    for _var_var_0 in ("reason_codes", "missing_inputs"):
        _var_var_4 = _var_var_3.get(_var_var_0)
        _var_var_3[_var_var_0] = (
            [
                _var_var_2
                for _var_var_2 in _var_var_4
                if isinstance(_var_var_2, str)
                if _SAFE_CODE.fullmatch(_var_var_2)
            ]
            if isinstance(_var_var_4, list)
            else []
        )
    for _var_var_0, _var_var_5 in _MOBILITY_CODE_VALUES.items():
        if _var_var_0 in _var_var_3:
            if _var_var_3[_var_var_0] is not None and (
                not isinstance(_var_var_3[_var_var_0], str)
                or _var_var_3[_var_var_0] not in _var_var_5
            ):
                _var_var_3.pop(_var_var_0)
    _var_var_6 = _var_var_3.get("hours")
    _var_var_3["hours"] = (
        [
            _var_var_8
            for _var_var_7 in _var_var_6
            if isinstance(_var_var_7, dict)
            if (
                _var_var_8 := {
                    _var_var_0: _var_var_2
                    for _var_var_0, _var_var_2 in _var_var_7.items()
                    if _var_var_0 in _MOBILITY_HOUR_PUBLIC_KEYS
                    if _var_var_0 != "allocation_status"
                    or (
                        isinstance(_var_var_2, str)
                        and _var_var_2 in _MOBILITY_HOUR_ALLOCATION_STATUS
                    )
                }
            )
        ]
        if isinstance(_var_var_6, list)
        else []
    )
    return _public_data(_var_var_3)
