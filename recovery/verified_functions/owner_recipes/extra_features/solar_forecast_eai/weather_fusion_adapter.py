# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
import inspect
from itertools import islice
from math import isfinite
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .weather_contract import normalize_provider_status, normalize_weather_source


def _project_source_comparison(value: Any) -> dict[str, list[dict[str, Any]]]:
    """Bound comparison tracks before recursive public DTO sanitization."""
    if not isinstance(value, dict):
        return {}
    _var_var_0 = {}
    for _var_var_1 in ("sfml_expert_blend", "kepler_input"):
        _var_var_2 = value.get(_var_var_1)
        if isinstance(_var_var_2, list):
            _var_var_0[_var_var_1] = [
                {
                    _var_var_4: _var_var_3[_var_var_4]
                    for _var_var_4 in _SOURCE_COMPARISON_FIELDS
                    if _var_var_4 in _var_var_3
                }
                for _var_var_3 in islice(_var_var_2, _PUBLIC_LIST_LIMITS[_var_var_1])
                if isinstance(_var_var_3, dict)
            ]
    return _var_var_0


class WeatherFusionAdapter:

    @staticmethod
    def _hourly(
        raw_hourly: Any,
        issued_at: datetime | None = None,
        local_timezone: ZoneInfo | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_hourly, list) or len(raw_hourly) > MAX_FORECAST_POINTS:
            return []
        _var_var_34 = []
        for _var_var_35 in raw_hourly:
            if (
                not isinstance(_var_var_35, dict)
                or len(_var_var_35) > MAX_FORECAST_POINT_FIELDS
            ):
                continue
            _var_var_36 = _as_datetime(
                _var_var_35.get("valid_at")
                or _var_var_35.get("datetime")
                or _var_var_35.get("timestamp")
                or _var_var_35.get("time"),
                local_timezone,
            )
            if _var_var_36 is None:
                continue
            if issued_at is not None:
                if (
                    not issued_at - timedelta(hours=1)
                    <= _var_var_36
                    <= issued_at + MAX_FORECAST_HORIZON
                ):
                    continue
            _var_var_37 = {}
            _var_var_38 = (
                _var_var_35.get("values")
                if isinstance(_var_var_35.get("values"), dict)
                else _var_var_35
            )
            if len(_var_var_38) > MAX_FORECAST_VALUE_FIELDS:
                continue
            for _var_var_39 in PUBLIC_FORECAST_METRICS:
                for _var_var_40 in PUBLIC_METRIC_INPUT_KEYS[_var_var_39]:
                    _var_var_41 = _metric_number(
                        _var_var_39, _var_var_38.get(_var_var_40)
                    )
                    if not _var_var_41 is not None:
                        continue
                    _var_var_37[_var_var_39] = _var_var_41
                    break
            _var_var_42 = _var_var_38.get("condition")
            if (
                isinstance(_var_var_42, str)
                and len(_var_var_42) <= 32
                and (_var_var_42 in PUBLIC_FORECAST_CONDITIONS)
            ):
                _var_var_37["condition"] = _var_var_42
            if not _var_var_37:
                continue
            _var_var_34.append({"valid_at": _var_var_36, "values": _var_var_37})
            continue
        _var_var_34.sort(key=lambda point: point["valid_at"])
        if len({_var_var_43["valid_at"] for _var_var_43 in _var_var_34}) != len(
            _var_var_34
        ):
            return []
        return _var_var_34
