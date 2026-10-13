# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import asyncio
import copy
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import math
import secrets
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from .grid_charge_gate import grid_charge_allowed
from .smc_enable import SMC_ENABLE_LEASE, async_set_enabled
from ..const import (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_SOC_SENSOR,
    CONF_EMS_HEAT_PUMP_BOOST_SWITCH,
    CONF_EMS_SURPLUS_SWITCH,
    CONF_EMS_WALLBOX_SWITCH,
    CONF_MAX_SOC,
    CONF_MIN_SOC,
    CONF_SMART_CHARGING_ENABLED,
    CONF_SMART_CHARGING_MODE,
    CONF_SMART_CHARGING_SWITCH,
    CONF_TARGET_SOC,
    CONF_SENSOR_BATTERY_SOC,
    DOMAIN,
)


class EMSManager:

    async def async_setup(self) -> None:
        """Restore preferences, always requiring a fresh auto-arm after restart."""
        _var_var_26 = await self._store.async_load()
        if isinstance(_var_var_26, dict):
            _var_var_27 = _var_var_26.get("enabled")
            if isinstance(_var_var_27, dict):
                for A_recovered_capture_0007 in EMS_ACTORS:
                    self.enabled[A_recovered_capture_0007] = bool(
                        _var_var_27.get(A_recovered_capture_0007, False)
                    )
            _var_var_28 = _var_var_26.get("audit")
            if isinstance(_var_var_28, list):
                self.audit.extend(
                    (
                        _var_var_39
                        for _var_var_39 in _var_var_28[-_AUDIT_LIMIT:]
                        if isinstance(_var_var_39, dict)
                    )
                )
            if _var_var_26.get("mode") in {"confirm", "automatic"}:
                self._audit("system", "restart_safe_observe", None, None, True)
            _var_var_29 = _var_var_26.get("owned_targets")
            if isinstance(_var_var_29, dict):
                _var_var_30 = {}
                for A_recovered_capture_0007, _var_var_31 in _var_var_29.items():
                    if A_recovered_capture_0007 not in EMS_ACTORS or not isinstance(
                        _var_var_31, str
                    ):
                        continue
                    if A_recovered_capture_0007 == "battery_grid_charge":
                        if _var_var_31 == SMC_ENABLE_LEASE:
                            _var_var_30[A_recovered_capture_0007] = _var_var_31
                        else:
                            self._audit(
                                "system",
                                "legacy_charge_switch_lease_dropped",
                                A_recovered_capture_0007,
                                None,
                                True,
                            )
                        continue
                    if _var_var_31.startswith("switch."):
                        _var_var_30[A_recovered_capture_0007] = _var_var_31
                self._owned_targets = _var_var_30
            _var_var_32 = _var_var_26.get("smc_enable_before")
            if isinstance(_var_var_32, bool):
                self._smc_enable_before = _var_var_32
            _var_var_33 = _var_var_26.get("smc_enable_set")
            if isinstance(_var_var_33, bool):
                self._smc_enable_set = _var_var_33
            self._user_smc_hold = _var_var_26.get("user_smc_hold") is True
            if self._user_smc_hold:
                self._owned_targets.pop("battery_grid_charge", None)
                self._smc_enable_before = None
                self._smc_enable_set = None
            _var_var_34 = _var_var_26.get("release_pending")
            if isinstance(_var_var_34, list):
                self._release_pending = {
                    A_recovered_capture_0007
                    for A_recovered_capture_0007 in _var_var_34
                    if A_recovered_capture_0007 in self._owned_targets
                }
            _var_var_35 = _var_var_26.get("paused_until")
            if isinstance(_var_var_35, dict):
                _var_var_36 = _utcnow()
                for A_recovered_capture_0007, _var_var_37 in _var_var_35.items():
                    _var_var_0 = _parse_time(_var_var_37)
                    if (
                        A_recovered_capture_0007 == "battery_grid_charge"
                        and self._user_smc_hold
                    ):
                        continue
                    if (
                        A_recovered_capture_0007 in EMS_ACTORS
                        and _var_var_0 is not None
                        and (_var_var_0 > _var_var_36)
                    ):
                        self._paused_until[A_recovered_capture_0007] = _var_var_0
                        _var_var_38 = _var_var_26.get("pause_reason")
                        if isinstance(_var_var_38, dict):
                            self._pause_reason[A_recovered_capture_0007] = str(
                                _var_var_38.get(A_recovered_capture_0007)
                                or "actor_paused"
                            )
        self.mode = "observe"
        if self._owned_targets:
            self._release_pending.update(self._owned_targets)
            await self._release_owned("startup_recovery")
        self._cancel_interval = async_track_time_interval(
            self.hass, self._scheduled_cycle, timedelta(minutes=1)
        )
        await self._save()

    async def async_control_cycle(self) -> None:
        """Run automatic decisions or retry a previously failed safe release."""
        _var_var_42 = bool(
            self.mode in {"confirm", "automatic"}
            or self._owned_targets
            or self._release_pending
        )
        if _var_var_42:
            async with self._lock:
                _var_var_43 = self._provider_snapshot()
                if _var_var_43 is None:
                    await self._disarm("license_or_freshness_lost")
                else:
                    _var_var_44 = self._live_payload(_var_var_43)
                    await self._disable_absent_actors(_var_var_44)
                    for A_recovered_capture_0006 in _var_var_44["actors"]:
                        if (
                            A_recovered_capture_0006["id"] == "battery_grid_charge"
                            and self._user_smc_hold
                        ):
                            self._owned_targets.pop("battery_grid_charge", None)
                            self._release_pending.discard("battery_grid_charge")
                            continue
                        if A_recovered_capture_0006["id"] in self._owned_targets:
                            if not (
                                A_recovered_capture_0006["enabled"]
                                and A_recovered_capture_0006["ready"]
                                and (A_recovered_capture_0006["desired"] is True)
                            ):
                                if (
                                    A_recovered_capture_0006["enabled"]
                                    and A_recovered_capture_0006["ready"]
                                    and (
                                        A_recovered_capture_0006["desired"] is not True
                                    )
                                ):
                                    _var_var_46 = self._on_since.get(
                                        str(A_recovered_capture_0006["id"])
                                    )
                                    _var_var_47 = _ACTOR_LIMITS[
                                        str(A_recovered_capture_0006["id"])
                                    ]
                                    if _var_var_46 is not None:
                                        if (
                                            _utcnow() - _var_var_46
                                            < _var_var_47.min_runtime
                                        ):
                                            continue
                                    if (
                                        A_recovered_capture_0006["id"]
                                        == "battery_grid_charge"
                                    ):
                                        if self._smc_flag():
                                            try:
                                                await self._execute_smc_enable(
                                                    False,
                                                    "scheduled_interlock_lost",
                                                    "system",
                                                )
                                            except EMSControlError:
                                                pass
                                        continue
                                await self._safe_off(
                                    A_recovered_capture_0006["id"],
                                    "scheduled_interlock_lost",
                                    "system",
                                )
                                continue
                if self._release_pending:
                    await self._release_owned("safe_release_retry")
                await self._save()
            if self._release_pending:
                return
        if self.mode == "automatic":
            await self.async_auto_cycle()
        return None if True else None

    def _recommendations(self) -> tuple[dict[str, Any], dict[str, Any]]:
        _var_var_48 = self.hass.data.get("solar_forecast_eai")
        if not isinstance(_var_var_48, dict):
            return ({}, {})
        for _var_var_57 in _var_var_48.values():
            _var_var_58 = getattr(_var_var_57, "recommendation_engine", None)
            if _var_var_58 is None or not callable(
                getattr(_var_var_58, "snapshot", None)
            ):
                continue
            try:
                _var_var_50 = _var_var_58.snapshot()
            except Exception:
                return ({}, {})
            return (
                _mapping(getattr(_var_var_50, "values", None)),
                _mapping(getattr(_var_var_50, "attributes", None)),
            )
        return ({}, {})

    def _energy_context(self) -> dict[str, Any]:
        for _var_var_52 in self.hass.data.get(DOMAIN, {}).values():
            if not isinstance(_var_var_52, dict):
                continue
            _var_var_49 = _var_var_52.get("energy_context_provider")
            if _var_var_49 is None:
                continue
            if not callable(getattr(_var_var_49, "snapshot", None)):
                continue
            try:
                return _mapping(_var_var_49.snapshot())
            except Exception:
                return {}
        return {}

    def _issue_tokens(self, payload: dict[str, Any], user_id: str) -> None:
        _var_var_36 = _utcnow()
        self._tokens = {
            _var_var_24: A_recovered_capture_0005
            for _var_var_24, A_recovered_capture_0005 in self._tokens.items()
            if A_recovered_capture_0005.expires_at > _var_var_36
        }
        for A_recovered_capture_0006 in payload["actors"]:
            _var_var_364 = str(A_recovered_capture_0006.pop("_input_stamp", ""))
            if not (
                self.mode == "confirm"
                and A_recovered_capture_0006["ready"]
                and A_recovered_capture_0006["enabled"]
            ):
                continue
            _var_var_365 = next(
                (
                    _var_var_24
                    for _var_var_24, A_recovered_capture_0005 in self._tokens.items()
                    if A_recovered_capture_0005.user_id == user_id
                    and A_recovered_capture_0005.actor_id
                    == A_recovered_capture_0006["id"]
                    and (
                        A_recovered_capture_0005.decision_id
                        == A_recovered_capture_0006["decision_id"]
                    )
                ),
                None,
            )
            _var_var_24 = _var_var_365 or secrets.token_urlsafe(32)
            if _var_var_365 is None:
                self._tokens[_var_var_24] = _Token(
                    user_id=user_id,
                    entry_id=self.entry.entry_id,
                    actor_id=A_recovered_capture_0006["id"],
                    desired=bool(A_recovered_capture_0006["desired"]),
                    decision_id=A_recovered_capture_0006["decision_id"],
                    input_stamp=_var_var_364,
                    expires_at=_var_var_36 + _TOKEN_TTL,
                )
            A_recovered_capture_0006["confirm_token"] = _var_var_24
            continue
        for _var_var_39 in payload.get("action_plan") or []:
            if isinstance(_var_var_39, dict):
                A_recovered_capture_0007 = _var_var_39.get("actor_id")
                _var_var_366 = next(
                    (
                        A_recovered_capture_0006
                        for A_recovered_capture_0006 in payload["actors"]
                        if A_recovered_capture_0006.get("id")
                        == A_recovered_capture_0007
                    ),
                    None,
                )
                _var_var_39["confirm_token"] = (
                    _var_var_366.get("confirm_token") if _var_var_366 else None
                )

    async def async_snapshot(self, user_id: str) -> dict[str, Any]:
        _var_var_43 = self._provider_snapshot()
        if _var_var_43 is None:
            if self.mode != "observe" or self._owned_targets or self._release_pending:
                async with self._lock:
                    await self._disarm("license_or_freshness_lost")
                    await self._save()
            _var_var_44 = self._mock_payload()
            _var_var_44["safety_alarm"] = bool(self._release_pending)
            return _var_var_44
        _var_var_44 = self._cached_live_payload(_var_var_43)
        _var_var_367 = any(
            (
                A_recovered_capture_0006.get("present") is not True
                and self.enabled.get(str(A_recovered_capture_0006.get("id")), False)
                for A_recovered_capture_0006 in _var_var_44["actors"]
            )
        )
        _var_var_368 = {
            A_recovered_capture_0006["id"]
            for A_recovered_capture_0006 in _var_var_44["actors"]
            if A_recovered_capture_0006["id"] in self._owned_targets
            if not A_recovered_capture_0006["enabled"]
            or not A_recovered_capture_0006["ready"]
            or A_recovered_capture_0006["desired"] is not True
        }
        if _var_var_368 or _var_var_367:
            async with self._lock:
                await self._disable_absent_actors(_var_var_44)
                for A_recovered_capture_0007 in _var_var_368:
                    if A_recovered_capture_0007 not in self._owned_targets:
                        continue
                    if (
                        A_recovered_capture_0007 == "battery_grid_charge"
                        and self.enabled.get(A_recovered_capture_0007)
                    ):
                        if not self._user_smc_hold:
                            A_recovered_capture_0006 = next(
                                (
                                    _var_var_39
                                    for _var_var_39 in _var_var_44["actors"]
                                    if _var_var_39["id"] == A_recovered_capture_0007
                                )
                            )
                            if (
                                A_recovered_capture_0006.get("ready")
                                and A_recovered_capture_0006.get("desired") is not True
                            ):
                                if self._smc_flag():
                                    try:
                                        await self._execute_smc_enable(
                                            False,
                                            "owned_intent_no_longer_safe",
                                            "system",
                                        )
                                    except EMSControlError:
                                        pass
                                continue
                    await self._safe_off(
                        A_recovered_capture_0007,
                        "owned_intent_no_longer_safe",
                        "system",
                    )
                await self._save()
            self._payload_cache = None
            _var_var_44 = self._cached_live_payload(_var_var_43)
        self._issue_tokens(_var_var_44, user_id)
        return self._apply_demo_features(_var_var_44)

    async def async_auto_cycle(self) -> None:
        if self.mode != "automatic":
            return
        async with self._lock:
            _var_var_50 = self._provider_snapshot()
            if _var_var_50 is None:
                await self._disarm("license_or_freshness_lost")
                await self._save()
                return
            _var_var_44 = self._live_payload(_var_var_50)
            await self._disable_absent_actors(_var_var_44)
            _var_var_155, _var_var_213 = self._surplus_state()
            _var_var_332 = bool(_var_var_155 is not None and _var_var_155.state == "on")
            _var_var_215 = (
                _number(
                    _mapping(getattr(_var_var_155, "attributes", None)).get(
                        "surplus_power_w"
                    ),
                    minimum=0,
                    maximum=1000000,
                )
                or 0.0
            )
            _var_var_369 = _var_var_215
            _var_var_36 = _utcnow()
            _var_var_301 = sorted(
                _var_var_44["actors"],
                key=lambda item: _ACTOR_PLAN_PRIORITY.get(str(item.get("id")), 9),
            )
            for A_recovered_capture_0006 in _var_var_301:
                A_recovered_capture_0007 = A_recovered_capture_0006["id"]
                _var_var_47 = _ACTOR_LIMITS[A_recovered_capture_0007]
                if A_recovered_capture_0007 == "battery_grid_charge":
                    self._drop_legacy_battery_lease()
                    if self._user_smc_hold:
                        continue
                _var_var_310 = self._paused_until.get(A_recovered_capture_0007)
                if _var_var_310 is not None:
                    if _var_var_310 > _var_var_36:
                        continue
                    self._paused_until.pop(A_recovered_capture_0007, None)
                    self._pause_reason.pop(A_recovered_capture_0007, None)
                if (
                    not A_recovered_capture_0006["enabled"]
                    or not A_recovered_capture_0006["ready"]
                ):
                    if A_recovered_capture_0007 in self._owned_targets:
                        await self._safe_off(
                            A_recovered_capture_0007,
                            "automatic_interlock_lost",
                            "system",
                        )
                    continue
                _var_var_298 = self._actor_current(A_recovered_capture_0007)
                _var_var_370 = self._owned_targets.get(A_recovered_capture_0007)
                if A_recovered_capture_0007 == "battery_grid_charge":
                    if _var_var_370 == SMC_ENABLE_LEASE:
                        if self._smc_enable_set is not None:
                            if _var_var_298 != self._smc_enable_set:
                                if _var_var_298 is False:
                                    self._mark_user_smc_hold()
                                    self._audit(
                                        "user",
                                        "user_smc_hold",
                                        A_recovered_capture_0007,
                                        _var_var_298,
                                        True,
                                    )
                                else:
                                    self._owned_targets.pop(
                                        A_recovered_capture_0007, None
                                    )
                                    self._release_pending.discard(
                                        A_recovered_capture_0007
                                    )
                                    self._smc_enable_before = None
                                    self._smc_enable_set = None
                                    self._user_smc_hold = False
                                await self._save()
                                continue
                elif _var_var_370 is not None and _var_var_298 is not None:
                    if _var_var_298 is not True:
                        self._pause_actor(A_recovered_capture_0007, "manual_override")
                        self._owned_targets.pop(A_recovered_capture_0007, None)
                        self._release_pending.discard(A_recovered_capture_0007)
                        self._audit(
                            "system",
                            "manual_override",
                            A_recovered_capture_0007,
                            _var_var_298,
                            True,
                        )
                        continue
                _var_var_164 = bool(A_recovered_capture_0006["desired"])
                if _var_var_164 is False and _var_var_298 is True:
                    if _var_var_370 is None:
                        if A_recovered_capture_0007 != "battery_grid_charge":
                            self._pause_actor(
                                A_recovered_capture_0007, "external_owner_paused"
                            )
                            self._tokens.clear()
                            self._audit(
                                "system",
                                "external_owner_paused",
                                A_recovered_capture_0007,
                                False,
                                True,
                            )
                            continue
                if _var_var_164 and _var_var_47.uses_surplus_budget:
                    if _var_var_332 or A_recovered_capture_0007 == "surplus_load":
                        if _var_var_298 is True:
                            _var_var_369 = max(
                                0.0, _var_var_369 - _var_var_47.nominal_power_w
                            )
                        else:
                            if _var_var_369 < _var_var_47.nominal_power_w:
                                self._audit(
                                    "system",
                                    "surplus_budget",
                                    A_recovered_capture_0007,
                                    True,
                                    False,
                                    surplus_w=_var_var_215,
                                )
                                continue
                            _var_var_369 -= _var_var_47.nominal_power_w
                if _var_var_164 is False and _var_var_298 is True:
                    _var_var_46 = self._on_since.get(A_recovered_capture_0007)
                    if _var_var_46 is not None:
                        if _var_var_36 - _var_var_46 < _var_var_47.min_runtime:
                            continue
                if _var_var_164 is True and _var_var_298 is False:
                    _var_var_371 = self._off_since.get(A_recovered_capture_0007)
                    if _var_var_371 is not None:
                        if _var_var_36 - _var_var_371 < _var_var_47.min_off_time:
                            continue
                if _var_var_298 == _var_var_164:
                    continue
                try:
                    await self._execute_switch(
                        A_recovered_capture_0007, _var_var_164, "automatic", "system"
                    )
                except EMSControlError as _var_var_372:
                    self._audit(
                        "system",
                        _var_var_372.code,
                        A_recovered_capture_0007,
                        _var_var_164,
                        False,
                    )
            await self._save()
        return None if True else None
