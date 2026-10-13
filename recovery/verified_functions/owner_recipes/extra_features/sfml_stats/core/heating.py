# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
from .heating_model import (
    OpenPhase,
    RoomModel,
    adopt_offset,
    close_phase,
    expected_temperature,
    forecast_summary,
    model_from_dict,
    model_to_dict,
    phase_kind,
    phase_to_dict,
    preheat_lead_hours,
    propose_offset,
    season_with_forecast,
    thermostat_demand,
)
import asyncio
import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, NamedTuple
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from ..const import DOMAIN, NAME, VERSION


def document_from_dict(
    payload: dict[str, Any] | None,
) -> tuple[
    HeatingSettings,
    list[RoomConfig],
    dict[str, dict[str, ThermoMemory]],
    datetime | None,
    str | None,
    tuple[tuple[datetime, float], ...],
    bool | None,
    dict[
        str,
        tuple[
            tuple[datetime, float | None, float | None, float | None, bool | None], ...
        ],
    ],
    dict[str, RoomModel],
    dict[str, OpenPhase],
    dict[str, dict[str, float]],
    dict[str, tuple[float, datetime]],
    frozenset[str],
    datetime | None,
    datetime | None,
    dict[str, GroupPresence],
]:
    if not isinstance(payload, dict):
        return (
            HeatingSettings(),
            [],
            {},
            None,
            None,
            (),
            None,
            {},
            {},
            {},
            {},
            {},
            frozenset(),
            None,
            None,
            {},
        )
    _var_var_154 = (
        payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    )
    try:
        _var_var_155 = validate_settings(_var_var_154, HeatingSettings())
    except HeatingError:
        _var_var_155 = HeatingSettings()
    _var_var_156 = []
    for _var_var_47 in payload.get("rooms") or []:
        if not isinstance(_var_var_47, dict) or not isinstance(
            _var_var_47.get("id"), str
        ):
            continue
        try:
            pass
        except HeatingError:
            continue
        try:
            _var_var_158 = validate_room(
                {
                    "name": _var_var_47.get("name") or _var_var_47["id"],
                    "climate_entities": _var_var_47.get("climate_entities") or [],
                    "temp_sensor": _var_var_47.get("temp_sensor"),
                    "window_sensor": _var_var_47.get("window_sensor"),
                    "comfort_temp_c": _var_var_47.get("comfort_temp_c", DEFAULT_BASE_C),
                    "setback_temp_c": _var_var_47.get("setback_temp_c"),
                },
                {_var_var_157.id for _var_var_157 in _var_var_156},
            )
        except HeatingError:
            continue
        _var_var_128, _var_var_129 = _stored_flags(_var_var_47)
        try:
            _var_var_130 = _schedule(_var_var_47.get("schedule") or [])
        except HeatingError:
            _var_var_130 = ()
        _var_var_131 = _var_var_47.get("auto_hvac") is not False
        _var_var_156.append(
            replace(
                _var_var_158,
                id=_var_var_47["id"],
                active=bool(_var_var_47.get("active")),
                presence_enabled=_var_var_128,
                schedule_enabled=_var_var_129,
                presence_persons=_stored_persons(_var_var_47),
                window_detect=_var_var_47.get("window_detect") is True,
                lift_enabled=_var_var_47.get("lift_enabled") is True,
                schedule=_var_var_130,
                auto_hvac=_var_var_131,
            )
        )
        continue
    _var_var_159 = {}
    _var_var_160 = payload.get("runtime")
    if isinstance(_var_var_160, dict):
        for _var_var_161, _var_var_74 in _var_var_160.items():
            if isinstance(_var_var_74, dict):
                _var_var_159[str(_var_var_161)] = {
                    A_recovered_capture_0000: memory_from_dict(_var_var_49)
                    for A_recovered_capture_0000, _var_var_49 in _var_var_74.items()
                    if isinstance(_var_var_49, dict)
                }
    _var_var_162 = payload.get("away_since")
    _var_var_163 = None
    if isinstance(_var_var_162, str):
        _var_var_163 = datetime.fromisoformat(_var_var_162)
        if _var_var_163.tzinfo is None:
            _var_var_163 = _var_var_163.replace(tzinfo=timezone.utc)
    _var_var_14 = payload.get("presence")
    _var_var_164 = []
    for _var_var_47 in payload.get("outdoor_samples") or []:
        if isinstance(_var_var_47, dict):
            _var_var_26 = _parse_stamp(_var_var_47.get("at"))
            _var_var_49 = _var_var_47.get("c")
            if (
                _var_var_26 is not None
                and isinstance(_var_var_49, (int, float))
                and (not isinstance(_var_var_49, bool))
            ):
                _var_var_164.append((_var_var_26, float(_var_var_49)))
    _var_var_165 = payload.get("heating_season")
    _var_var_166 = _var_var_14 if isinstance(_var_var_14, str) else None
    _var_var_167 = _groups_from_dict(payload, _var_var_155, _var_var_163, _var_var_166)
    _var_var_168 = _var_var_167[presence_group_key(_var_var_155.presence_entities)]
    _var_var_169, _var_var_170 = _boosts_from_dict(payload, _utcnow())
    _var_var_171, _var_var_172 = _lift_since_from_dict(payload.get("lift_since"))
    return (
        _var_var_155,
        _var_var_156,
        _var_var_159,
        _var_var_168.away_since,
        _var_var_168.presence,
        tuple(_var_var_164),
        _var_var_165 if isinstance(_var_var_165, bool) else None,
        _room_samples_from_dict(payload.get("room_samples")),
        _models_from_dict(payload.get("room_models")),
        _phases_from_dict(payload.get("open_phases")),
        _offsets_from_dict(payload.get("thermostat_offsets")),
        _var_var_169,
        _var_var_170,
        _var_var_171,
        _var_var_172,
        _var_var_167,
    )


class HeatingManager:

    async def _refresh_forecast(self, now: datetime) -> None:
        if self._forecast_at is not None and now - self._forecast_at < timedelta(
            minutes=30
        ):
            return
        self._forecast_at = now
        try:
            from pathlib import Path as _var_var_220
            from ..readers.weather_reader import WeatherDataReader as _var_var_221

            _var_var_222 = _var_var_221(_var_var_220(self.hass.config.path()))
            _var_var_177 = _forecast_zone(self.hass)
            _var_var_11, _var_var_12 = _forecast_local_span(now, _var_var_177)
            _var_var_223 = await _var_var_222.async_get_forecast_weather(
                _var_var_11, _var_var_12
            )
        except Exception:
            self._forecast_mean = None
            self._forecast_min_12h = None
            return
        _var_var_224 = []
        for _var_var_225 in _var_var_223:
            if not _is_number(_var_var_225.temperature_c):
                continue
            _var_var_26 = _local_hour_to_utc(
                _var_var_225.date, int(_var_var_225.hour), _var_var_177
            )
            if _var_var_26 is None:
                continue
            _var_var_224.append((_var_var_26, float(_var_var_225.temperature_c)))
        self._forecast_points = tuple(_var_var_224)
        self._forecast_mean, self._forecast_min_12h = forecast_summary(
            _var_var_224, now
        )


def _phases_from_dict(payload: Any) -> dict[str, OpenPhase]:
    _var_var_23 = {}
    if not isinstance(payload, dict):
        return _var_var_23
    for _var_var_161, _var_var_47 in payload.items():
        if not isinstance(_var_var_47, dict) or _var_var_47.get("kind") not in {
            "cool",
            "heat",
        }:
            continue
        _var_var_3 = _parse_stamp(_var_var_47.get("started"))
        _var_var_267 = _var_var_47.get("start_c")
        if not _var_var_3 is not None or not _is_number(_var_var_267):
            continue
        _var_var_117 = _var_var_47.get("outdoor_c")
        _var_var_268 = (
            _var_var_47.get("offsets")
            if isinstance(_var_var_47.get("offsets"), dict)
            else {}
        )
        _var_var_193 = tuple(
            (
                (str(A_recovered_capture_0000), float(_var_var_49))
                for A_recovered_capture_0000, _var_var_49 in _var_var_268.items()
                if _is_number(_var_var_49)
            )
        )
        _var_var_23[str(_var_var_161)] = OpenPhase(
            str(_var_var_47["kind"]),
            _var_var_3,
            float(_var_var_267),
            float(_var_var_117) if _is_number(_var_var_117) else None,
            _var_var_193,
        )
        continue
    return _var_var_23


def _entity_classes() -> tuple[type, type]:
    _var_var_276 = getattr(_entity_classes, "cached", None)
    if _var_var_276 is not None:
        return _var_var_276
    from homeassistant.components.sensor import (
        SensorDeviceClass as A_recovered_capture_0004,
        SensorEntity as _var_var_277,
        SensorStateClass as A_recovered_capture_0005,
    )
    from homeassistant.components.switch import SwitchEntity as _var_var_278
    from homeassistant.const import UnitOfTemperature as A_recovered_capture_0006
    from homeassistant.helpers.entity import DeviceInfo as A_recovered_capture_0003

    class _HeatingEntity:
        _attr_has_entity_name = True
        _attr_should_poll = False

        def __init__(self, manager: HeatingManager, room_id: str) -> None:
            self.manager = manager
            self.room_id = room_id
            self._attr_device_info = A_recovered_capture_0003(
                identifiers={(DOMAIN, manager.entry.entry_id)},
                name=NAME,
                manufacturer="Zara-Toorox",
                model="Solar Forecast Stats",
                sw_version=VERSION,
            )

        @property
        def available(self) -> bool:
            return True

        def _room(self) -> RoomConfig:
            return self.manager._room(self.room_id)

        def _decision(self) -> RoomDecision | None:
            return self.manager.decisions.get(self.room_id)

    class HeatingTargetSensor(_HeatingEntity, _var_var_277):
        _attr_device_class = A_recovered_capture_0004.TEMPERATURE
        _attr_state_class = A_recovered_capture_0005.MEASUREMENT
        _attr_native_unit_of_measurement = A_recovered_capture_0006.CELSIUS
        _attr_translation_key = "heating_target"

        def __init__(self, manager: HeatingManager, room_id: str) -> None:
            super().__init__(manager, room_id)
            self._attr_unique_id = f"{manager.entry.entry_id}_heating_{room_id}_target"

        @property
        def native_value(self) -> float | None:
            _var_var_85 = self._decision()
            return _var_var_85.target_c if _var_var_85 else None

        @property
        def extra_state_attributes(self) -> dict[str, Any]:
            _var_var_85 = self._decision()
            _var_var_101 = None
            if _var_var_85 is not None:
                _var_var_90 = [
                    _var_var_47.memory.last_write_at
                    for _var_var_47 in _var_var_85.thermostats
                    if _var_var_47.memory.last_write_at is not None
                ]
                if _var_var_90:
                    _var_var_101 = max(_var_var_90).isoformat()
            _var_var_255 = None
            self__recovered_capture_0022 = self.manager.boosts.get(self.room_id)
            if (
                self__recovered_capture_0022 is not None
                and self__recovered_capture_0022[1] > _utcnow()
            ):
                _var_var_255 = self__recovered_capture_0022[1].isoformat()
            _var_var_61 = self.manager.detected_until.get(self.room_id)
            return (
                (
                    (
                        {
                            "mode": _var_var_85.mode if _var_var_85 else "observe",
                            "reason": (
                                _var_var_85.reason if _var_var_85 else "room_inactive"
                            ),
                            "presence": (
                                _var_var_85.presence
                                if _var_var_85
                                else self.manager.presence
                            ),
                            "last_write": _var_var_101,
                            "boost_until": _var_var_255,
                            "window_detected": _var_var_61 is not None
                            and _var_var_61 > _utcnow(),
                            "lift": (
                                _var_var_85.reason
                                if _var_var_85
                                and _var_var_85.reason in {"pv_lift", "price_lift"}
                                else None
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

    class HeatingActiveSwitch(_HeatingEntity, _var_var_278):
        _attr_translation_key = "heating_active"

        def __init__(self, manager: HeatingManager, room_id: str) -> None:
            super().__init__(manager, room_id)
            self._attr_unique_id = f"{manager.entry.entry_id}_heating_{room_id}_active"

        @property
        def is_on(self) -> bool:
            try:
                return self._room().active
            except HeatingError:
                return False

        async def async_turn_on(self, **kwargs: Any) -> None:
            await self.manager.async_set_active(self.room_id, True)

        async def async_turn_off(self, **kwargs: Any) -> None:
            await self.manager.async_set_active(self.room_id, False)

    _entity_classes.cached = (HeatingTargetSensor, HeatingActiveSwitch)
    return (HeatingTargetSensor, HeatingActiveSwitch)
