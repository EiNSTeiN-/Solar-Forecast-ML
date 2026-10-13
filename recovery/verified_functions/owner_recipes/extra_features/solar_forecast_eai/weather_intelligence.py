# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
from math import isfinite, sqrt
from typing import Any
from zoneinfo import ZoneInfo
from .weather_contract import normalize_weather_source
from .weather_forecast_store import ForecastSnapshotStore
from .weather_fusion_adapter import SFMLWeatherHistoryAdapter, WeatherFusionAdapter
from .weather_station_adapter import SFMLWeatherStationAdapter


class WeatherIntelligenceEngine:

    @staticmethod
    def _latest_actual(envelope: Any, reference_now: datetime) -> dict[str, Any] | None:
        if not isinstance(envelope, dict) or envelope.get("status") not in {
            "ok",
            "partial",
            "available",
        }:
            return None
        _var_var_120 = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(_var_var_120, list):
            return
        _var_var_121 = {
            "temperature": "temperature_c",
            "wind_direction": "wind_bearing",
        }
        for _var_var_43 in reversed(_var_var_120):
            if not isinstance(_var_var_43, dict) or not isinstance(
                _var_var_43.get("values"), dict
            ):
                continue
            _var_var_4 = _var_var_43.get("observed_at")
            if isinstance(_var_var_4, str):
                try:
                    _var_var_4 = _as_utc(_var_var_4)
                except ValueError:
                    continue
            if not isinstance(_var_var_4, datetime):
                continue
            _var_var_4 = _var_var_4.astimezone(timezone.utc)
            _var_var_122 = reference_now.astimezone(timezone.utc)
            if (
                _var_var_4 > _var_var_122 + _CURRENT_FUTURE_TOLERANCE
                or _var_var_122 - _var_var_4 > _CURRENT_MAX_AGE
            ):
                continue
            _var_var_44 = {
                _var_var_121.get(_var_var_7, _var_var_7): _var_var_22
                for _var_var_7, _var_var_22 in _var_var_43["values"].items()
                if _var_var_7 in _PUBLIC_ACTUAL_METRICS
                if isinstance(_var_var_22, (int, float))
                if not isinstance(_var_var_22, bool)
                if isfinite(_var_var_22)
            }
            if not _var_var_44:
                continue
            return {"observed_at": _var_var_4, "values": _var_var_44}
