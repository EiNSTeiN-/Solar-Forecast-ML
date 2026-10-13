# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Licensed EMS decision aggregation and fail-closed actuator execution."""
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

EMS_MODES = frozenset({"confirm", "observe", "automatic"})
EMS_ACTORS = frozenset(
    {"wallbox", "surplus_load", "heat_pump_boost", "battery_grid_charge"}
)
_TOKEN_TTL = timedelta(minutes=2)
_MAX_SOURCE_AGE = timedelta(minutes=15)
_SURPLUS_MAX_AGE = timedelta(minutes=3)
_PRICE_MAX_AGE = timedelta(minutes=60)
_FORECAST_MAX_AGE = timedelta(hours=6)
_COOLDOWN = timedelta(minutes=3)
_MAX_CYCLES_PER_HOUR = 8
_AUDIT_LIMIT = 250
_SERVICE_TIMEOUT = 10.0
_PAYLOAD_CACHE_TTL = 10.0
_MOCK_CACHE_TTL = 30.0
_HORIZON_HOURS = 36
_MANUAL_PAUSE = timedelta(hours=2)
_FLAT_TARIFF_SPREAD = 0.04
_AUDIT_DETAIL_KEYS = frozenset(
    {"mode", "label", "surplus_w", "decision_id", "soc_percent", "price_ct_kwh"}
)
_NOTIFY_DISARM_REASONS = frozenset({"license_or_freshness_lost"})
_USER_SMC_OFF = "Smart Charging ist von dir ausgeschaltet. Das EMS greift nicht ein, bis du es wieder einschaltest."


class EMSControlError(ValueError):
    """A bounded EMS control error suitable for the authenticated API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class _Token:
    user_id: str
    entry_id: str
    actor_id: str
    desired: bool
    decision_id: str
    input_stamp: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _ActorSpec:
    config_key: str
    label: str
    source: str


_ACTOR_SPECS = {
    "battery_grid_charge": _ActorSpec(
        CONF_SMART_CHARGING_SWITCH, "Smart Charging", "Smart Charging"
    ),
    "surplus_load": _ActorSpec(
        CONF_EMS_SURPLUS_SWITCH, "Überschussverbraucher", "Überschuss"
    ),
    "wallbox": _ActorSpec(CONF_EMS_WALLBOX_SWITCH, "Wallbox", "Mobilität"),
    "heat_pump_boost": _ActorSpec(
        CONF_EMS_HEAT_PUMP_BOOST_SWITCH, "Wärmepumpe", "Wärme"
    ),
}
_ACTOR_PLAN_PRIORITY = {
    "wallbox": 0,
    "heat_pump_boost": 1,
    "surplus_load": 2,
    "battery_grid_charge": 3,
}


@dataclass(frozen=True, slots=True)
class _ActorLimits:
    nominal_power_w: float
    min_surplus_w: float
    min_runtime: timedelta
    min_off_time: timedelta
    uses_surplus_budget: bool


_ACTOR_LIMITS = {
    "surplus_load": _ActorLimits(
        1500.0, 400.0, timedelta(minutes=5), timedelta(minutes=3), True
    ),
    "wallbox": _ActorLimits(
        3700.0, 1500.0, timedelta(minutes=5), timedelta(minutes=3), True
    ),
    "heat_pump_boost": _ActorLimits(
        1500.0, 800.0, timedelta(minutes=15), timedelta(minutes=10), True
    ),
    "battery_grid_charge": _ActorLimits(
        0.0, 0.0, timedelta(minutes=5), timedelta(minutes=3), False
    ),
}
_PLAN_ON = {
    "battery_grid_charge": "Netzladung empfohlen",
    "surplus_load": "Überschussverbrauch empfohlen",
    "wallbox": "Laden empfohlen",
    "heat_pump_boost": "Wärmepumpe jetzt nutzen",
}
_PLAN_OFF = {
    "battery_grid_charge": "Jetzt nicht aus dem Netz laden",
    "surplus_load": "Überschuss jetzt nicht schalten",
    "wallbox": "Jetzt nicht laden",
    "heat_pump_boost": "Wärmepumpe jetzt nicht zuschalten",
}
_REASON_LABELS = {
    "license_required": "Ohne Freigabe bleibt die Steuerung gesperrt.",
    "no_decision": "Noch keine belastbare Empfehlung.",
    "price_too_high": "Diese Stunde ist nicht günstig.",
    "price_above_threshold": "Diese Stunde ist nicht günstig.",
    "price_unavailable": "Der Preis fehlt oder ist von der vorigen Stunde, deshalb bleibt die Netzladung aus.",
    "price_demo": "Beispieldaten, deshalb bleibt die Netzladung aus.",
    "force_price_charge_start": "Der Preis liegt unter dem Force-Preis, der Akku lädt bis zur Obergrenze.",
    "force_price_charge_continuous": "Der Preis liegt unter dem Force-Preis, der Akku lädt bis zur Obergrenze.",
    "later_grid_import_is_cheaper": "Später aus dem Netz zu beziehen ist billiger als jetzt zu speichern.",
    "later_cheap_slots_insufficient_capacity": "Späterer, billigerer Netzbezug reicht nicht für die ganze Ladung.",
    "economic_input_unavailable": "Für die Ladeentscheidung fehlen Angaben.",
    "forecast_data_unavailable": "Akkustand, Verbrauch oder Solarprognose fehlen.",
    "forecast_covers_expected_demand": "Akku und Sonne reichen für die nächsten anderthalb Tage.",
    "charge_window_unavailable": "In dieser Stunde lässt sich keine Netzladung ausführen.",
    "demand_deadline_unavailable_charge_now": "Ohne Zeitraum für den Bedarf wird in dieser günstigen Stunde geladen.",
    "future_price_forecast_unavailable": "Für die kommenden Stunden liegt kein nutzbarer Strompreis vor.",
    "charge_now_avoids_costlier_later_import": "Bis der Bedarf gedeckt ist, gibt es keine günstigere Stunde.",
    "smart_charging_disabled": "Die Netzladung ist ausgeschaltet.",
    "gpm_unavailable": "Ohne Grid Price Monitor bleibt die Netzladung aus.",
    "soc_unavailable": "Der Akkustand fehlt, deshalb gibt es keine Netzladung.",
    "soc_unavailable_force_charge_blocked": "Der Akkustand fehlt, deshalb gibt es keine Netzladung.",
    "soc_unavailable_standard_charge_blocked": "Der Akkustand fehlt, deshalb gibt es keine Netzladung.",
    "daytime_standard_charge_disabled": "Tagsüber bleibt dieser Weg aus, damit Platz für die Sonne bleibt.",
    "soc_reached_target_or_hysteresis": "Der Akkustand hat den Zielstand erreicht.",
    "standard_charge_below_target_continuous": "Es ist Nacht, die Stunde ist günstig und die Netzladung läuft weiter.",
    "standard_charge_below_target_start": "Es ist Nacht, die Stunde ist günstig und die Netzladung startet.",
    "band_soc_reached": "Der Akkustand hat den Zielstand erreicht.",
    "band_force_below_max_soc_continuous": "Der Preis liegt unter dem Force-Preis, der Akku lädt bis zur Obergrenze.",
    "band_force_below_max_soc_start": "Der Preis liegt unter dem Force-Preis, der Akku lädt bis zur Obergrenze.",
    "band_charge_below_target_continuous": "Die Stunde ist günstig, der Akkustand liegt unter dem Zielstand und die Netzladung läuft weiter.",
    "band_charge_below_target_start": "Die Stunde ist günstig, der Akkustand liegt unter dem Zielstand und die Netzladung startet.",
    "battery_capacity_missing": "Die Batteriekapazität fehlt, deshalb gibt es keine Netzladung.",
    "disabled": "Die Netzladung ist ausgeschaltet.",
    "unknown": "Der Grund wird noch ermittelt.",
    "signal_unavailable": "Das Überschusssignal fehlt gerade.",
    "external_owner": "Ein anderer Automationspfad steuert diesen Schalter bereits.",
    "data_unavailable": "Die nötigen Sensordaten fehlen noch.",
    "not_load": "Netzladung ist gerade nicht sinnvoll.",
    "load": "Günstiger Zeitraum für Netzladung.",
    "charge": "Laden ist jetzt sinnvoll.",
    "heating": "Heizen mit günstigem Strom oder PV-Überschuss.",
    "dhw": "Warmwasser jetzt nachladen.",
    "thermal_storage": "Wärme speichern, solange Strom günstig oder reichlich ist.",
    "actor_paused": "EMS pausiert dieses Gerät nach einem manuellen Eingriff.",
    "user_smc_off": _USER_SMC_OFF,
    "user_smc_hold": _USER_SMC_OFF,
    "user_smc_resumed": "Smart Charging ist wieder eingeschaltet. Das EMS darf wieder eingreifen.",
    "legacy_charge_switch_lease_dropped": "Alte Netzladeschalter-Übernahme verworfen.",
    "surplus_budget": "Kein freies Leistungsbudget für diesen Verbraucher.",
    "min_runtime": "Mindestlaufzeit ist noch nicht erreicht.",
    "min_off_time": "Mindestpause nach dem letzten Ausschalten.",
    "fixed_tariff": "Festtarif: keine günstigen oder teuren Preisfenster.",
    "service_timeout": "Der Schaltaufruf hat das Zeitlimit überschritten.",
}
_PRIORITY_LABELS = {
    "household_base_load": "Haus",
    "household": "Haus",
    "heat_pump": "Wärmepumpe",
    "battery_reserve": "Akku",
    "battery": "Akku",
    "calibration_reserve": "Kalibrierreserve",
    "wallbox": "Wallbox",
    "grid_export": "Netzeinspeisung",
}
_PRIORITY_REQUIRED = {
    "household_base_load": "household",
    "household": "household",
    "heat_pump": "heat_pump",
    "battery_reserve": "battery",
    "battery": "battery",
    "wallbox": "wallbox",
    "grid_export": "household",
}
_ALLOCATION_ROWS = (
    ("household_pv_kwh", "Haus", "solar", "PV direkt in den Hausbedarf"),
    ("heat_pump_pv_kwh", "Wärmepumpe", "heat", "PV nach Hausbedarf, vor Speicher"),
    (
        "battery_pv_reserved_kwh",
        "Akku-Reserve",
        "battery",
        "PV, die der Speicher zuerst bekommt",
    ),
    (
        "residual_pv_kwh",
        "Überschuss",
        "mobility",
        "Übrig nach den vorhandenen Verbrauchern",
    ),
    (
        "unallocated_or_export_kwh",
        "Export",
        "export",
        "Nicht zugeordnet oder Einspeisung",
    ),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed_time = value
    elif isinstance(value, str) and value:
        try:
            parsed_time = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return
    else:
        return
    if parsed_time.tzinfo is None:
        parsed_time = parsed_time.replace(tzinfo=timezone.utc)
    return parsed_time.astimezone(timezone.utc)


def _number(
    value: Any, *, minimum: float = -1000000, maximum: float = 1000000
) -> float | None:
    if isinstance(value, bool):
        return
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return
    if not (math.isfinite(numeric_value) and minimum <= numeric_value <= maximum):
        return None if True else None
    return round(numeric_value, 3)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _humanize_reason(value: Any) -> str:
    reason_text = str(value or "").strip()
    if not reason_text:
        return "Keine Begründung verfügbar."
    reason_label = _REASON_LABELS.get(reason_text)
    if reason_label:
        return reason_label
    humanized_reason = reason_text.replace("_", " ").strip()
    if humanized_reason:
        return humanized_reason[0].upper() + humanized_reason[1:]
    return "Keine Begründung verfügbar."


def _hour_label(start_hour: int, end_hour: int) -> str:
    return f"{start_hour:02d}–{end_hour + 1:02d} Uhr"


def _upcoming_window_hour(when_start: Any, now_local: datetime) -> int | None:
    """Return the start hour when the window begins after the current local hour."""
    if not isinstance(when_start, str) or "T" not in when_start:
        return None
    date_str, time_str = when_start.split("T", 1)
    try:
        hour = int(time_str[:2])
        year, month, day_number = (
            int(date_part) for date_part in date_str.split("-", 2)
        )
    except (TypeError, ValueError):
        return
    if (year, month, day_number, hour) <= (
        now_local.year,
        now_local.month,
        now_local.day,
        now_local.hour,
    ):
        return
    return hour


def _battery_customer_text(
    *,
    reason: str,
    desired: bool | None,
    storage_cost: float | None,
    later_hour: int | None,
) -> str:
    """One catalog sentence. Cost and the later hour stay off this line."""
    del storage_cost, later_hour
    reason_label = _REASON_LABELS.get(reason)
    if reason_label:
        return reason_label
    if desired is True:
        return "Lädt aus dem Netz."
    if desired is False:
        return "Lädt nicht."
    return "Keine Aktion nötig."


def _battery_status_lines(
    *,
    reason: str,
    active: bool,
    grid_kind: str,
    mode: str | None,
    target_soc: Any,
    max_soc: Any,
    min_soc: Any,
    next_cheap_hour: Any,
    under_force: bool,
    now_hour: int | None,
    observe: bool,
) -> list[str]:
    """Three customer lines. The middle line is the catalog sentence."""
    from .charging_status import status_facts as status_facts

    battery_decision = status_facts(
        active=active,
        reason=reason,
        grid_kind=grid_kind,
        mode=mode,
        target_soc=target_soc,
        max_soc=max_soc,
        min_soc=min_soc,
        next_cheap_hour=next_cheap_hour,
        under_force=under_force,
        now_hour=now_hour,
        observe=observe,
    )
    status_lines = [
        (
            "Lädt aus dem Netz."
            if battery_decision["headline"] == "charging"
            else "Lädt nicht."
        )
    ]
    reason_label = _REASON_LABELS.get(str(battery_decision["why_code"]))
    status_lines.append(
        reason_label if reason_label else "Der Grund wird noch ermittelt."
    )
    upcoming_window = (
        battery_decision.get("next")
        if isinstance(battery_decision.get("next"), dict)
        else {}
    )
    upcoming_kind = upcoming_window.get("kind")
    if upcoming_kind == "observe":
        status_lines.append("Beobachten schaltet nicht.")
    elif upcoming_kind == "stop_hour":
        status_lines.append("Stopp zum Stundenende.")
    elif upcoming_kind == "stop_soc" and upcoming_window.get("soc") is not None:
        status_lines.append(
            f"Stopp bei {int(upcoming_window['soc'])} % oder zum Stundenende."
        )
    elif upcoming_kind == "next_hour" and upcoming_window.get("hour") is not None:
        hour = int(upcoming_window["hour"])
        if upcoming_window.get("soc") is not None:
            status_lines.append(
                f"Nächste günstige Stunde ab {hour:02d}:00. Lädt dann bis {int(upcoming_window['soc'])} %."
            )
        else:
            status_lines.append(f"Nächste günstige Stunde ab {hour:02d}:00.")
    return status_lines if True else None


def _battery_dev_text(
    *,
    reason: str,
    storage_cost: float | None,
    future_price: float | None,
    efficiency: float | None,
) -> str:
    """Technical comparison for the dev-only intelligence card.

    ``future_price`` is the deadline minimum published as
    ``smart_charging_compared_future_price_ct_kwh``. It is not a slot the
    reserve window is guaranteed to use.
    """
    diagnostic_lines = [f"Planergrund: {reason or 'unbekannt'}."]
    if storage_cost is not None:
        diagnostic_lines.append(
            f"Netzladung jetzt {storage_cost:.1f} ct/kWh inklusive Verluste."
        )
    if future_price is not None:
        diagnostic_lines.append(
            f"Veröffentlichtes Horizont-Minimum {future_price:.1f} ct/kWh, nicht zwingend ein nutzbarer Slot innerhalb der Reserve."
        )
    if efficiency is not None:
        diagnostic_lines.append(f"Roundtrip {efficiency:.0f} %.")
    return " ".join(diagnostic_lines)


def _fresh(
    stamp: datetime | None, now: datetime, max_age: timedelta | None = None
) -> bool:
    source_max_age = max_age if max_age is not None else _MAX_SOURCE_AGE
    return stamp is not None and timedelta(0) <= now - stamp <= source_max_age


def _dispatch_surplus_kwh(
    point: dict[str, Any], components: dict[str, Any]
) -> float | None:
    residual_pv_kwh = _number(point.get("residual_pv_kwh"), minimum=0, maximum=1000)
    if residual_pv_kwh is not None:
        return residual_pv_kwh
    pv_after_direct_kwh = _number(
        point.get("pv_potential_after_direct_kwh"), minimum=0, maximum=1000
    )
    if pv_after_direct_kwh is None:
        return
    if not components.get("battery"):
        return pv_after_direct_kwh
    battery_pv_reserved_kwh = _number(
        point.get("battery_pv_reserved_kwh"), minimum=0, maximum=1000
    )
    if battery_pv_reserved_kwh is None:
        return pv_after_direct_kwh
    return round(max(pv_after_direct_kwh - battery_pv_reserved_kwh, 0.0), 3)


def _price_spread_ratio(prices: list[float]) -> float | None:
    if len(prices) < 2:
        return 0.0 if prices else None
    median_price_ct_kwh = sorted(prices)[len(prices) // 2]
    if median_price_ct_kwh == 0:
        return 0.0 if max(prices) == min(prices) else 1.0
    return (max(prices) - min(prices)) / abs(median_price_ct_kwh)


def _why_fits_components(text: str, components: dict[str, Any]) -> bool:
    folded_reason_text = text.casefold()
    if not components.get("heat_pump"):
        if any(
            (
                search_term in folded_reason_text
                for search_term in (
                    "wärmepumpe",
                    "warmwasser",
                    "thermischen speicher",
                    "heat pump",
                )
            )
        ):
            return False
    if not components.get("wallbox"):
        if any(
            (
                search_term in folded_reason_text
                for search_term in ("wallbox", "abfahrt", "fahrzeug", "sofortladen")
            )
        ):
            return False
    if not components.get("battery"):
        if any(
            (
                search_term in folded_reason_text
                for search_term in ("speicherstand", "netzladung", "roundtrip")
            )
        ):
            return False
    return True


class EMSManager:
    """Aggregate decisions and serialize every Home Assistant service call."""

    def __init__(self, hass: Any, entry: Any, config: dict[str, Any]) -> None:
        self.hass = hass
        self.entry = entry
        self.config = dict(config)
        self.mode = "observe"
        self.enabled = {actor_id: False for actor_id in EMS_ACTORS}
        self.audit = deque(maxlen=_AUDIT_LIMIT)
        self._tokens = {}
        self._used_tokens = deque(maxlen=256)
        self._lock = asyncio.Lock()
        self._cancel_interval = None
        self._owned_targets = {}
        self._release_pending = set()
        self._last_action = {}
        self._cycles = {actor_id: deque() for actor_id in EMS_ACTORS}
        self._on_since = {}
        self._off_since = {}
        self._paused_until = {}
        self._pause_reason = {}
        self._smc_enable_before = None
        self._smc_enable_set = None
        self._user_smc_hold = False
        self._payload_cache = None
        self._mock_cache = None
        self._store = Store(hass, 1, f"{DOMAIN}.ems.{entry.entry_id}")

    async def async_setup(self) -> None:
        """Restore preferences, always requiring a fresh auto-arm after restart."""
        saved_state = await self._store.async_load()
        if isinstance(saved_state, dict):
            saved_enabled_flags = saved_state.get("enabled")
            if isinstance(saved_enabled_flags, dict):
                for actor_id in EMS_ACTORS:
                    self.enabled[actor_id] = bool(
                        saved_enabled_flags.get(actor_id, False)
                    )
            saved_audit = saved_state.get("audit")
            if isinstance(saved_audit, list):
                self.audit.extend(
                    (
                        item
                        for item in saved_audit[-_AUDIT_LIMIT:]
                        if isinstance(item, dict)
                    )
                )
            if saved_state.get("mode") in {"confirm", "automatic"}:
                self._audit("system", "restart_safe_observe", None, None, True)
            saved_owned_targets = saved_state.get("owned_targets")
            if isinstance(saved_owned_targets, dict):
                validated_owned_targets = {}
                for actor_id, target_entity_id in saved_owned_targets.items():
                    if actor_id not in EMS_ACTORS or not isinstance(
                        target_entity_id, str
                    ):
                        continue
                    if actor_id == "battery_grid_charge":
                        if target_entity_id == SMC_ENABLE_LEASE:
                            validated_owned_targets[actor_id] = target_entity_id
                        else:
                            self._audit(
                                "system",
                                "legacy_charge_switch_lease_dropped",
                                actor_id,
                                None,
                                True,
                            )
                        continue
                    if target_entity_id.startswith("switch."):
                        validated_owned_targets[actor_id] = target_entity_id
                self._owned_targets = validated_owned_targets
            saved_smc_enable_before = saved_state.get("smc_enable_before")
            if isinstance(saved_smc_enable_before, bool):
                self._smc_enable_before = saved_smc_enable_before
            saved_smc_enable_set = saved_state.get("smc_enable_set")
            if isinstance(saved_smc_enable_set, bool):
                self._smc_enable_set = saved_smc_enable_set
            self._user_smc_hold = saved_state.get("user_smc_hold") is True
            if self._user_smc_hold:
                self._owned_targets.pop("battery_grid_charge", None)
                self._smc_enable_before = None
                self._smc_enable_set = None
            saved_release_pending = saved_state.get("release_pending")
            if isinstance(saved_release_pending, list):
                self._release_pending = {
                    actor_id
                    for actor_id in saved_release_pending
                    if actor_id in self._owned_targets
                }
            saved_paused_until = saved_state.get("paused_until")
            if isinstance(saved_paused_until, dict):
                now_utc = _utcnow()
                for actor_id, paused_until_value in saved_paused_until.items():
                    parsed_time = _parse_time(paused_until_value)
                    if actor_id == "battery_grid_charge" and self._user_smc_hold:
                        continue
                    if (
                        actor_id in EMS_ACTORS
                        and parsed_time is not None
                        and (parsed_time > now_utc)
                    ):
                        self._paused_until[actor_id] = parsed_time
                        saved_pause_reasons = saved_state.get("pause_reason")
                        if isinstance(saved_pause_reasons, dict):
                            self._pause_reason[actor_id] = str(
                                saved_pause_reasons.get(actor_id) or "actor_paused"
                            )
        self.mode = "observe"
        if self._owned_targets:
            self._release_pending.update(self._owned_targets)
            await self._release_owned("startup_recovery")
        self._cancel_interval = async_track_time_interval(
            self.hass, self._scheduled_cycle, timedelta(minutes=1)
        )
        await self._save()

    async def async_shutdown(self) -> bool:
        """Stop scheduling and release only switches that EMS itself enabled."""
        async with self._lock:
            await self._disarm("integration_unload")
            await self._save()
            if self._owned_targets or self._release_pending:
                return False
        if self._cancel_interval is not None:
            self._cancel_interval()
            self._cancel_interval = None
        return True

    def resume_after_failed_unload(self) -> None:
        """Restore the safety timer when Home Assistant aborts platform unload."""
        if self._cancel_interval is None:
            self._cancel_interval = async_track_time_interval(
                self.hass, self._scheduled_cycle, timedelta(minutes=1)
            )
        return None if True else None

    async def async_update_config(self, config: dict[str, Any]) -> None:
        """Release old concrete targets before accepting a new allowlist."""
        async with self._lock:
            previous_smc_enabled = self._smc_flag()
            await self._disarm("config_changed")
            current_smc_enabled = self._smc_flag()
            self.config = dict(config)
            if (
                previous_smc_enabled != current_smc_enabled
                and "battery_grid_charge" not in self._release_pending
            ):
                self.config[CONF_SMART_CHARGING_ENABLED] = current_smc_enabled
            self._tokens.clear()
            await self._save()
        return None if True else None

    async def _scheduled_cycle(self, _now: datetime) -> None:
        """Run the interval callback on Home Assistant's event loop."""
        await self.async_control_cycle()

    async def async_control_cycle(self) -> None:
        """Run automatic decisions or retry a previously failed safe release."""
        needs_control_cycle = bool(
            self.mode in {"confirm", "automatic"}
            or self._owned_targets
            or self._release_pending
        )
        if needs_control_cycle:
            async with self._lock:
                provider_snapshot = self._provider_snapshot()
                if provider_snapshot is None:
                    await self._disarm("license_or_freshness_lost")
                else:
                    payload = self._live_payload(provider_snapshot)
                    await self._disable_absent_actors(payload)
                    for actor in payload["actors"]:
                        if actor["id"] == "battery_grid_charge" and self._user_smc_hold:
                            self._owned_targets.pop("battery_grid_charge", None)
                            self._release_pending.discard("battery_grid_charge")
                            continue
                        if actor["id"] in self._owned_targets:
                            if not (
                                actor["enabled"]
                                and actor["ready"]
                                and (actor["desired"] is True)
                            ):
                                if (
                                    actor["enabled"]
                                    and actor["ready"]
                                    and (actor["desired"] is not True)
                                ):
                                    on_since = self._on_since.get(str(actor["id"]))
                                    actor_limits = _ACTOR_LIMITS[str(actor["id"])]
                                    if on_since is not None:
                                        if (
                                            _utcnow() - on_since
                                            < actor_limits.min_runtime
                                        ):
                                            continue
                                    if actor["id"] == "battery_grid_charge":
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
                                    actor["id"], "scheduled_interlock_lost", "system"
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

    async def _disable_absent_actors(self, payload: dict[str, Any]) -> None:
        """Revoke opt-ins when a configured customer component disappears."""
        for actor in payload.get("actors", []):
            actor_id = actor.get("id")
            if (
                actor_id not in EMS_ACTORS
                or actor.get("present") is True
                or (not self.enabled.get(actor_id))
            ):
                continue
            if actor_id in self._owned_targets:
                await self._safe_off(actor_id, "actor_presence_lost", "system")
            self.enabled[actor_id] = False
            self._tokens.clear()
            self._audit(
                "system",
                "actor_presence_lost",
                actor_id,
                False,
                actor_id not in self._owned_targets,
            )

    def _provider_snapshot(self) -> dict[str, Any] | None:
        eai_data = self.hass.data.get("solar_forecast_eai")
        if not isinstance(eai_data, dict):
            return
        capability_provider = eai_data.get("capability_provider")
        if capability_provider is None or not callable(
            getattr(capability_provider, "ems_snapshot", None)
        ):
            return
        try:
            snapshot = capability_provider.ems_snapshot()
        except Exception:
            return
        if not isinstance(snapshot, dict):
            return
        if (
            not snapshot.get("license_status") == "valid"
            or not snapshot.get("data_mode") == "live"
            or snapshot.get("provider_error") is not None
            or (snapshot.get("premium_full_package") is not True)
        ):
            return None
        data_freshness_at = _parse_time(snapshot.get("data_freshness"))
        if not _fresh(data_freshness_at, _utcnow()):
            return
        return snapshot

    def _gpm(self) -> tuple[dict[str, Any], datetime | None, bool]:
        for source_entry in self.hass.data.get(DOMAIN, {}).values():
            if not isinstance(source_entry, dict):
                continue
            gpm_coordinator = source_entry.get("gpm_coordinator")
            if not gpm_coordinator is not None:
                continue
            gpm_data = _mapping(getattr(gpm_coordinator, "data", None))
            gpm_updated_at = _parse_time(
                getattr(gpm_coordinator, "last_update_success_time", None)
            )
            if gpm_updated_at is None:
                gpm_updated_at = _parse_time(
                    gpm_data.get("last_update") or gpm_data.get("timestamp")
                )
            return (
                gpm_data,
                gpm_updated_at,
                bool(getattr(gpm_coordinator, "last_update_success", False)),
            )
        return ({}, None, False)

    def _gpm_coordinator(self) -> Any | None:
        for source_entry in self.hass.data.get(DOMAIN, {}).values():
            if not isinstance(source_entry, dict):
                continue
            gpm_coordinator = source_entry.get("gpm_coordinator")
            if not gpm_coordinator is not None:
                continue
            return gpm_coordinator

    def _grid_price_allows_charge(self) -> bool:
        """Cheap hour or a price below the force price. Stale and demo stay closed."""
        gpm_data, gpm_updated_at, gpm_fresh = self._gpm()
        if not gpm_fresh:
            return False
        return grid_charge_allowed(
            gpm_data,
            datetime.now(self._local_timezone()),
            coordinator=self._gpm_coordinator(),
        )

    def _recommendations(self) -> tuple[dict[str, Any], dict[str, Any]]:
        eai_data = self.hass.data.get("solar_forecast_eai")
        if not isinstance(eai_data, dict):
            return ({}, {})
        for eai_coordinator in eai_data.values():
            recommendation_engine = getattr(
                eai_coordinator, "recommendation_engine", None
            )
            if recommendation_engine is None or not callable(
                getattr(recommendation_engine, "snapshot", None)
            ):
                continue
            try:
                snapshot = recommendation_engine.snapshot()
            except Exception:
                return ({}, {})
            return (
                _mapping(getattr(snapshot, "values", None)),
                _mapping(getattr(snapshot, "attributes", None)),
            )
        return ({}, {})

    def _surplus_state(self) -> tuple[Any | None, datetime | None]:
        surplus_entity_id = "binary_sensor.solar_forecast_stats_pv_surplus_available"
        entity_state = self.hass.states.get(surplus_entity_id)
        if entity_state is None:
            binary_sensor_states = getattr(
                self.hass.states, "async_all", lambda *_args: []
            )("binary_sensor")
            entity_state = next(
                (
                    candidate_state
                    for candidate_state in binary_sensor_states
                    if str(getattr(candidate_state, "entity_id", "")).endswith(
                        "pv_surplus_available"
                    )
                ),
                None,
            )
        return (
            (
                (
                    (
                        entity_state,
                        (
                            _parse_time(getattr(entity_state, "last_updated", None))
                            if entity_state
                            else None
                        ),
                    )
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )

    def _actor_state(self, entity_id: str | None) -> bool | None:
        if not (isinstance(entity_id, str) and entity_id.startswith("switch.")):
            return None
        entity_state = self.hass.states.get(entity_id)
        if entity_state is None or entity_state.state not in {"on", "off"}:
            return None
        return entity_state.state == "on"

    def _energy_context(self) -> dict[str, Any]:
        for source_entry in self.hass.data.get(DOMAIN, {}).values():
            if not isinstance(source_entry, dict):
                continue
            energy_context_provider = source_entry.get("energy_context_provider")
            if energy_context_provider is None:
                continue
            if not callable(getattr(energy_context_provider, "snapshot", None)):
                continue
            try:
                return _mapping(energy_context_provider.snapshot())
            except Exception:
                return {}
        return {}

    def _eai_feature_config(self) -> tuple[bool, bool]:
        eai_data = self.hass.data.get("solar_forecast_eai")
        if isinstance(eai_data, dict):
            for eai_coordinator in eai_data.values():
                config_entry = getattr(eai_coordinator, "entry", None)
                if not config_entry is not None:
                    continue
                feature_config = {
                    **getattr(config_entry, "data", {}),
                    **getattr(config_entry, "options", {}),
                }
                return (
                    feature_config.get("heat_pump_enabled") is True,
                    feature_config.get("wallbox_enabled") is True,
                )
        config_entries = getattr(self.hass, "config_entries", None)
        get_config_entries = getattr(config_entries, "async_entries", None)
        if callable(get_config_entries):
            for config_entry in get_config_entries("solar_forecast_eai"):
                feature_config = {
                    **getattr(config_entry, "data", {}),
                    **getattr(config_entry, "options", {}),
                }
                return (
                    feature_config.get("heat_pump_enabled") is True,
                    feature_config.get("wallbox_enabled") is True,
                )
        return (False, False)

    def _component_presence(self) -> dict[str, bool]:
        feature_config, has_eai_config = self._eai_feature_config()
        battery_soc_entity = self.config.get(
            CONF_BATTERY_SOC_SENSOR
        ) or self.config.get(CONF_SENSOR_BATTERY_SOC)
        return {
            "household": True,
            "heat_pump": feature_config,
            "wallbox": has_eai_config,
            "battery": isinstance(battery_soc_entity, str)
            and bool(battery_soc_entity.strip()),
        }

    def _actor_present(self, actor_id: str, components: dict[str, bool]) -> bool:
        target_entity_id = self.config.get(_ACTOR_SPECS[actor_id].config_key)
        is_switch_target = isinstance(
            target_entity_id, str
        ) and target_entity_id.startswith("switch.")
        if not is_switch_target:
            return False
        if actor_id == "battery_grid_charge":
            return bool(components["battery"])
        if actor_id == "wallbox":
            return components["wallbox"]
        if actor_id == "heat_pump_boost":
            return components["heat_pump"]
        return True

    def _local_timezone(self) -> ZoneInfo:
        timezone_name = str(
            getattr(getattr(self.hass, "config", None), "time_zone", "UTC")
        )
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    def _normalize_hours(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        snapshot_sections = _mapping(snapshot.get("sections"))
        mobility_hours = _mapping(snapshot_sections.get("mobility")).get("hours")
        forecast_hours = _mapping(snapshot_sections.get("forecast")).get("hours")
        if not isinstance(forecast_hours, list) or not forecast_hours:
            return []
        source_forecast_hours = forecast_hours
        mobility_by_time = {}
        if isinstance(mobility_hours, list):
            for source_hour in mobility_hours[:72]:
                mobility_point = _mapping(source_hour)
                mobility_start = _parse_time(
                    mobility_point.get("interval_start")
                    or mobility_point.get("timestamp")
                )
                if mobility_start is not None:
                    mobility_by_time[mobility_start.isoformat()] = mobility_point
        energy_context = self._energy_context()
        observed_by_date_hour = {}
        observed_hours = energy_context.get("observed_hours")
        if isinstance(observed_hours, list):
            for source_hour in observed_hours[:25]:
                observed_point = _mapping(source_hour)
                date_str = observed_point.get("date")
                hour = _number(observed_point.get("hour"), minimum=0, maximum=23)
                if isinstance(date_str, str):
                    if hour is not None and hour.is_integer():
                        observed_by_date_hour[date_str, int(hour)] = observed_point
        price_by_hour = {}
        price_rows_by_hour = {}
        price_hours = energy_context.get("price_hours")
        if isinstance(price_hours, list):
            for source_hour in price_hours[:72]:
                price_point = _mapping(source_hour)
                price_timestamp = _parse_time(price_point.get("timestamp"))
                if price_timestamp is not None:
                    price_time_local = price_timestamp.astimezone(
                        self._local_timezone()
                    )
                    price_by_hour[price_timestamp.isoformat()] = price_point
                    price_rows_by_hour.setdefault(
                        (price_time_local.date().isoformat(), price_time_local.hour), []
                    ).append(price_point)
        load_profiles = (
            energy_context.get("profile")
            if isinstance(energy_context.get("profile"), list)
            else []
        )
        profile_by_weekday_hour = {
            (
                int(source_point.get("weekday")),
                int(source_point.get("hour")),
            ): source_point
            for source_point in load_profiles
            if isinstance(source_point, dict)
            if source_point.get("weekday") is not None
            if source_point.get("hour") is not None
        }
        components = self._component_presence()
        local_timezone = self._local_timezone()
        observed_keys_used = set()
        normalized_hours = []
        for source_hour in source_forecast_hours[:72]:
            source_point = _mapping(source_hour)
            timestamp_value = source_point.get("interval_start") or source_point.get(
                "timestamp"
            )
            parsed_time = _parse_time(timestamp_value)
            if parsed_time is None:
                continue
            local_time = parsed_time.astimezone(local_timezone)
            date_hour_key = (local_time.date().isoformat(), local_time.hour)
            observed_values = observed_by_date_hour.get(date_hour_key, {})
            battery_actual_kwh = _number(
                observed_values.get("battery_to_house_kwh"), minimum=0, maximum=1000
            )
            has_battery_actual = battery_actual_kwh is not None
            unique_observed_values = (
                observed_values if date_hour_key not in observed_keys_used else {}
            )
            if unique_observed_values:
                observed_keys_used.add(date_hour_key)
            price_point = price_by_hour.get(parsed_time.isoformat(), {})
            if not price_point:
                hour_price_rows = price_rows_by_hour.get(date_hour_key, [])
                price_candidates = [
                    _number(
                        price_sample.get("price_ct_kwh"), minimum=-1000, maximum=5000
                    )
                    for price_sample in hour_price_rows
                ]
                valid_prices = [
                    price_sample
                    for price_sample in price_candidates
                    if price_sample is not None
                ]
                if valid_prices:
                    price_point = {
                        "price_ct_kwh": round(sum(valid_prices) / len(valid_prices), 3)
                    }
            mobility_point = mobility_by_time.get(parsed_time.isoformat(), {})
            load_profile = _mapping(
                profile_by_weekday_hour.get((local_time.weekday(), local_time.hour))
            )
            pv_forecast_kwh = _number(
                source_point.get("pv_forecast_kwh"), minimum=0, maximum=1000
            )
            household_kwh = _number(
                source_point.get("household_base_load_kwh"), minimum=0, maximum=1000
            )
            if household_kwh is None and load_profile.get("available") is True:
                household_kwh = _number(
                    load_profile.get("household_base_load_kwh"), minimum=0, maximum=1000
                )
            heat_pump_kwh = (
                _number(source_point.get("heat_pump_kwh"), minimum=0, maximum=1000)
                if components["heat_pump"]
                else 0.0
            )
            wallbox_kwh = None
            if components["wallbox"]:
                wallbox_kwh = _number(
                    mobility_point.get("wallbox_kwh"), minimum=0, maximum=1000
                )
                if wallbox_kwh is None:
                    wallbox_kwh = _number(
                        mobility_point.get("pv_wallbox_kwh"), minimum=0, maximum=1000
                    )
                if wallbox_kwh is None:
                    wallbox_kwh = _number(
                        source_point.get("wallbox_kwh"), minimum=0, maximum=1000
                    )
                if wallbox_kwh is None:
                    wallbox_kwh = _number(
                        source_point.get("pv_wallbox_kwh"), minimum=0, maximum=1000
                    )
            else:
                wallbox_kwh = 0.0
            battery_reserve_kwh = (
                _number(
                    source_point.get("battery_reserve_kwh"), minimum=0, maximum=1000
                )
                if components["battery"]
                else 0.0
            )
            battery_to_house_actual_kwh = (
                battery_actual_kwh
                if components["battery"] and unique_observed_values
                else None
            )
            battery_to_house_forecast_kwh = (
                _number(
                    load_profile.get("battery_to_house_forecast_kwh"),
                    minimum=0,
                    maximum=1000,
                )
                if components["battery"] and (not has_battery_actual)
                else None
            )
            price_ct_kwh = _number(
                price_point.get("price_ct_kwh"), minimum=-1000, maximum=5000
            )
            if price_ct_kwh is None:
                price_ct_kwh = _number(
                    unique_observed_values.get("price_ct_kwh"),
                    minimum=-1000,
                    maximum=5000,
                )
            demand_components_kwh = (household_kwh, heat_pump_kwh, wallbox_kwh)
            demand_kwh = (
                None
                if any(
                    (component_kwh is None for component_kwh in demand_components_kwh)
                )
                else round(household_kwh + heat_pump_kwh + wallbox_kwh, 3)
            )
            if battery_to_house_actual_kwh is not None and demand_kwh is not None:
                battery_to_house_actual_kwh = round(
                    min(battery_to_house_actual_kwh, demand_kwh), 3
                )
            if battery_to_house_forecast_kwh is not None and demand_kwh is not None:
                battery_to_house_forecast_kwh = round(
                    min(battery_to_house_forecast_kwh, demand_kwh), 3
                )
            battery_discharge_kwh = (
                battery_to_house_actual_kwh
                if battery_to_house_actual_kwh is not None
                else battery_to_house_forecast_kwh
            )
            covered_kwh = (
                None
                if pv_forecast_kwh is None or demand_kwh is None
                else round(min(pv_forecast_kwh, demand_kwh), 3)
            )
            battery_coverage_kwh = (
                battery_discharge_kwh
                if components["battery"] and battery_discharge_kwh is not None
                else 0.0 if not components["battery"] or has_battery_actual else None
            )
            grid_import_kwh = (
                None
                if demand_kwh is None
                or covered_kwh is None
                or battery_coverage_kwh is None
                else round(max(demand_kwh - covered_kwh - battery_coverage_kwh, 0.0), 3)
            )
            grid_cost_eur = (
                None
                if grid_import_kwh is None or price_ct_kwh is None
                else round(grid_import_kwh * price_ct_kwh / 100, 4)
            )
            pv_after_direct_kwh = (
                None
                if pv_forecast_kwh is None or covered_kwh is None
                else round(max(pv_forecast_kwh - covered_kwh, 0.0), 3)
            )
            coverage_percent = (
                None
                if demand_kwh in {None, 0} or covered_kwh is None
                else round(covered_kwh / demand_kwh * 100, 1)
            )
            household_pv_kwh = _number(
                source_point.get("household_pv_kwh"), minimum=0, maximum=1000
            )
            heat_pump_pv_kwh = (
                _number(source_point.get("heat_pump_pv_kwh"), minimum=0, maximum=1000)
                if components["heat_pump"]
                else 0.0
            )
            battery_pv_reserved_kwh = (
                _number(
                    source_point.get("battery_pv_reserved_kwh"), minimum=0, maximum=1000
                )
                if components["battery"]
                else 0.0
            )
            if components["battery"] and battery_pv_reserved_kwh is None:
                battery_pv_reserved_kwh = _number(
                    source_point.get("battery_pv_reserve_kwh"), minimum=0, maximum=1000
                )
            residual_pv_kwh = _number(
                source_point.get("residual_pv_kwh"), minimum=0, maximum=1000
            )
            if residual_pv_kwh is None:
                residual_pv_kwh = _number(
                    source_point.get("wallbox_pv_available_kwh"),
                    minimum=0,
                    maximum=1000,
                )
            normalized_hours.append(
                {
                    "timestamp": parsed_time.isoformat(),
                    "pv_kwh": pv_forecast_kwh,
                    "demand_kwh": demand_kwh,
                    "covered_kwh": covered_kwh,
                    "household_kwh": household_kwh,
                    "heat_pump_kwh": heat_pump_kwh,
                    "wallbox_kwh": wallbox_kwh,
                    "battery_kwh": battery_reserve_kwh,
                    "battery_to_house_kwh": battery_discharge_kwh,
                    "battery_to_house_actual_kwh": battery_to_house_actual_kwh,
                    "battery_to_house_forecast_kwh": battery_to_house_forecast_kwh,
                    "battery_to_house_origin": (
                        "stats_hourly_billing_actual"
                        if battery_to_house_actual_kwh is not None
                        else (
                            str(
                                load_profile.get("battery_to_house_profile_origin")
                                or "unavailable"
                            )
                            if battery_to_house_forecast_kwh is not None
                            else "unavailable"
                        )
                    ),
                    "price_ct_kwh": price_ct_kwh,
                    "expected_grid_import_kwh": grid_import_kwh,
                    "expected_grid_cost_eur": grid_cost_eur,
                    "pv_potential_after_direct_kwh": pv_after_direct_kwh,
                    "coverage_percent": coverage_percent,
                    "confidence_percent": _number(
                        source_point.get("confidence_percent"), minimum=0, maximum=100
                    ),
                    "uncertainty_percent": _number(
                        source_point.get("uncertainty_percent"), minimum=0, maximum=100
                    ),
                    "allocation_valid": source_point.get("allocation_valid") is True,
                    "household_pv_kwh": household_pv_kwh,
                    "heat_pump_pv_kwh": heat_pump_pv_kwh,
                    "battery_pv_reserved_kwh": battery_pv_reserved_kwh,
                    "residual_pv_kwh": residual_pv_kwh,
                    "historical_grid_import_kwh": _number(
                        load_profile.get("historical_grid_import_kwh"),
                        minimum=0,
                        maximum=1000,
                    ),
                }
            )
            continue
        return sorted(normalized_hours, key=lambda item: item["timestamp"])

    @staticmethod
    def _available_total(points: list[dict[str, Any]], key: str) -> float | None:
        values = [
            hour_point.get(key)
            for hour_point in points
            if hour_point.get(key) is not None
        ]
        if not values:
            return
        return round(sum((float(energy_value) for energy_value in values)), 2)

    @staticmethod
    def _merge_calendar_slot(points: list[dict[str, Any]], hour: int) -> dict[str, Any]:
        """Aggregate repeated DST-fold intervals into one visual hour slot."""
        merged_slot = dict(points[0])
        for energy_field in (
            "pv_kwh",
            "demand_kwh",
            "covered_kwh",
            "household_kwh",
            "heat_pump_kwh",
            "wallbox_kwh",
            "battery_kwh",
            "expected_grid_import_kwh",
            "expected_grid_cost_eur",
            "pv_potential_after_direct_kwh",
            "household_pv_kwh",
            "heat_pump_pv_kwh",
            "battery_pv_reserved_kwh",
            "residual_pv_kwh",
            "historical_grid_import_kwh",
        ):
            field_values = [hour_point.get(energy_field) for hour_point in points]
            merged_slot[energy_field] = (
                round(sum((float(field_value) for field_value in field_values)), 3)
                if all((field_value is not None for field_value in field_values))
                else None
            )
        battery_actual_values = [
            float(hour_point["battery_to_house_actual_kwh"])
            for hour_point in points
            if hour_point.get("battery_to_house_actual_kwh") is not None
        ]
        battery_forecast_values = [
            float(hour_point["battery_to_house_forecast_kwh"])
            for hour_point in points
            if hour_point.get("battery_to_house_forecast_kwh") is not None
        ]
        merged_slot["battery_to_house_actual_kwh"] = (
            round(sum(battery_actual_values), 3) if battery_actual_values else None
        )
        merged_slot["battery_to_house_forecast_kwh"] = (
            round(sum(battery_forecast_values), 3)
            if battery_forecast_values and (not battery_actual_values)
            else None
        )
        merged_slot["battery_to_house_kwh"] = (
            merged_slot["battery_to_house_actual_kwh"]
            if merged_slot["battery_to_house_actual_kwh"] is not None
            else merged_slot["battery_to_house_forecast_kwh"]
        )
        merged_slot["battery_to_house_origin"] = (
            "stats_hourly_billing_actual"
            if battery_actual_values
            else str(points[0].get("battery_to_house_origin") or "unavailable")
        )
        demand_kwh = merged_slot.get("demand_kwh")
        covered_kwh = merged_slot.get("covered_kwh")
        merged_slot["coverage_percent"] = (
            round(covered_kwh / demand_kwh * 100, 1)
            if demand_kwh not in {None, 0} and covered_kwh is not None
            else None
        )
        price_values = [
            float(hour_point["price_ct_kwh"])
            for hour_point in points
            if hour_point.get("price_ct_kwh") is not None
        ]
        merged_slot["price_ct_kwh"] = (
            round(sum(price_values) / len(price_values), 3) if price_values else None
        )
        confidence_values = [
            float(hour_point["confidence_percent"])
            for hour_point in points
            if hour_point.get("confidence_percent") is not None
        ]
        uncertainty_values = [
            float(hour_point["uncertainty_percent"])
            for hour_point in points
            if hour_point.get("uncertainty_percent") is not None
        ]
        merged_slot["confidence_percent"] = (
            round(sum(confidence_values) / len(confidence_values), 1)
            if confidence_values
            else None
        )
        merged_slot["uncertainty_percent"] = (
            round(sum(uncertainty_values) / len(uncertainty_values), 1)
            if uncertainty_values
            else None
        )
        merged_slot["allocation_valid"] = all(
            (hour_point.get("allocation_valid") is True for hour_point in points)
        )
        merged_slot["hour"] = hour
        merged_slot["interval_count"] = len(points)
        return merged_slot

    def _calendar_days(self, hours: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build local calendar days with explicit missing hourly slots."""
        local_timezone = self._local_timezone()
        today_local_date = _utcnow().astimezone(local_timezone).date()
        points_by_date_hour = {}
        for hour_point in hours:
            parsed_time = _parse_time(hour_point.get("timestamp"))
            if parsed_time is not None:
                local_time = parsed_time.astimezone(local_timezone)
                if local_time.date() < today_local_date:
                    continue
                points_by_date_hour.setdefault(
                    local_time.date().isoformat(), {}
                ).setdefault(local_time.hour, []).append(hour_point)
        if not points_by_date_hour:
            points_by_date_hour[today_local_date.isoformat()] = {}
        days = []
        for date_str in sorted(points_by_date_hour)[:3]:
            year, month, day_number = (
                int(date_part) for date_part in date_str.split("-")
            )
            hour_points_by_hour = points_by_date_hour[date_str]
            hour_slots = []
            for hour in range(24):
                slot_points = hour_points_by_hour.get(hour, [])
                if slot_points:
                    hour_point = self._merge_calendar_slot(slot_points, hour)
                else:
                    slot_timestamp = datetime(
                        year, month, day_number, hour, tzinfo=local_timezone
                    ).isoformat()
                    hour_point = {
                        "timestamp": slot_timestamp,
                        "hour": hour,
                        "interval_count": 0,
                        "pv_kwh": None,
                        "demand_kwh": None,
                        "covered_kwh": None,
                        "household_kwh": None,
                        "heat_pump_kwh": None,
                        "wallbox_kwh": None,
                        "battery_kwh": None,
                        "battery_to_house_kwh": None,
                        "battery_to_house_actual_kwh": None,
                        "battery_to_house_forecast_kwh": None,
                        "battery_to_house_origin": "unavailable",
                        "price_ct_kwh": None,
                        "expected_grid_import_kwh": None,
                        "expected_grid_cost_eur": None,
                        "pv_potential_after_direct_kwh": None,
                        "coverage_percent": None,
                        "confidence_percent": None,
                        "uncertainty_percent": None,
                        "allocation_valid": False,
                        "household_pv_kwh": None,
                        "heat_pump_pv_kwh": None,
                        "battery_pv_reserved_kwh": None,
                        "residual_pv_kwh": None,
                        "historical_grid_import_kwh": None,
                    }
                hour_slots.append(hour_point)
            valid_coverage_slots = [
                hour_point
                for hour_point in hour_slots
                if hour_point.get("pv_kwh") is not None
                if hour_point.get("demand_kwh") is not None
                if hour_point.get("covered_kwh") is not None
            ]
            daily_pv_kwh = self._available_total(valid_coverage_slots, "pv_kwh")
            daily_demand_kwh = self._available_total(valid_coverage_slots, "demand_kwh")
            daily_covered_kwh = self._available_total(
                valid_coverage_slots, "covered_kwh"
            )
            grid_slots = [
                hour_point
                for hour_point in hour_slots
                if hour_point.get("expected_grid_import_kwh") is not None
            ]
            priced_grid_slots = [
                hour_point
                for hour_point in grid_slots
                if hour_point.get("expected_grid_cost_eur") is not None
            ]
            day_summary = {
                "pv_kwh": daily_pv_kwh,
                "demand_kwh": daily_demand_kwh,
                "covered_kwh": daily_covered_kwh,
                "coverage_percent": (
                    round(daily_covered_kwh / daily_demand_kwh * 100, 1)
                    if daily_demand_kwh and daily_covered_kwh is not None
                    else None
                ),
                "household_kwh": self._available_total(
                    valid_coverage_slots, "household_kwh"
                ),
                "heat_pump_kwh": self._available_total(
                    valid_coverage_slots, "heat_pump_kwh"
                ),
                "wallbox_kwh": self._available_total(
                    valid_coverage_slots, "wallbox_kwh"
                ),
                "battery_kwh": self._available_total(
                    valid_coverage_slots, "battery_kwh"
                ),
                "battery_to_house_kwh": self._available_total(
                    hour_slots, "battery_to_house_kwh"
                ),
                "battery_to_house_actual_kwh": self._available_total(
                    hour_slots, "battery_to_house_actual_kwh"
                ),
                "battery_to_house_forecast_kwh": self._available_total(
                    hour_slots, "battery_to_house_forecast_kwh"
                ),
                "battery_observed_hours": sum(
                    (
                        hour_point.get("battery_to_house_actual_kwh") is not None
                        for hour_point in hour_slots
                    )
                ),
                "battery_forecast_hours": sum(
                    (
                        hour_point.get("battery_to_house_forecast_kwh") is not None
                        for hour_point in hour_slots
                    )
                ),
                "average_price_ct_kwh": (
                    round(sum(price_sample) / len(price_sample), 2)
                    if (
                        price_sample := [
                            float(hour_point["price_ct_kwh"])
                            for hour_point in hour_slots
                            if hour_point.get("price_ct_kwh") is not None
                        ]
                    )
                    else None
                ),
                "expected_grid_import_kwh": self._available_total(
                    grid_slots, "expected_grid_import_kwh"
                ),
                "grid_import_hours": len(grid_slots),
                "expected_grid_cost_eur": self._available_total(
                    priced_grid_slots, "expected_grid_cost_eur"
                ),
                "grid_cost_hours": len(priced_grid_slots),
                "grid_cost_complete": len(priced_grid_slots) == 24,
                "pv_potential_after_direct_kwh": self._available_total(
                    valid_coverage_slots, "pv_potential_after_direct_kwh"
                ),
                "household_pv_kwh": self._available_total(
                    valid_coverage_slots, "household_pv_kwh"
                ),
                "heat_pump_pv_kwh": self._available_total(
                    valid_coverage_slots, "heat_pump_pv_kwh"
                ),
                "battery_pv_reserved_kwh": self._available_total(
                    valid_coverage_slots, "battery_pv_reserved_kwh"
                ),
                "residual_pv_kwh": self._available_total(
                    valid_coverage_slots, "residual_pv_kwh"
                ),
                "forecast_confidence_percent": (
                    round(sum(confidence_sample) / len(confidence_sample))
                    if (
                        confidence_sample := [
                            float(hour_point["confidence_percent"])
                            for hour_point in hour_slots
                            if hour_point.get("confidence_percent") is not None
                        ]
                    )
                    else None
                ),
            }
            coverage_slot_count = len(valid_coverage_slots)
            days.append(
                {
                    "date": date_str,
                    "relative_day": (
                        datetime(year, month, day_number).date() - today_local_date
                    ).days,
                    "hours": hour_slots,
                    "summary": day_summary,
                    "complete_hours": coverage_slot_count,
                    "interval_count": sum(
                        (
                            int(hour_point.get("interval_count", 0))
                            for hour_point in hour_slots
                        )
                    ),
                    "complete": coverage_slot_count == 24,
                    "partial": coverage_slot_count < 24,
                }
            )
            continue
        return days

    @staticmethod
    def _attach_expected_autarky(
        days: list[dict[str, Any]], context: dict[str, Any], *, synthetic: bool = False
    ) -> None:
        """Attach autonomy using STATS' measured loss factor without hiding origin."""
        loss_adjusted_autarky_percent = _number(
            context.get("loss_adjusted_autarky_percent"), minimum=0, maximum=100
        )
        system_efficiency_percent = _number(
            context.get("system_efficiency_percent"), minimum=0, maximum=100
        )
        if synthetic:
            system_efficiency_percent = 92.0
        for day in days:
            day_summary = _mapping(day.get("summary"))
            coverage_percent = _number(
                day_summary.get("coverage_percent"), minimum=0, maximum=100
            )
            if (
                not synthetic
                and day.get("relative_day") == 0
                and (loss_adjusted_autarky_percent is not None)
            ):
                expected_autarky_percent = loss_adjusted_autarky_percent
                autarky_origin = "stats_loss_adjusted_actual"
            elif coverage_percent is not None and system_efficiency_percent is not None:
                expected_autarky_percent = round(
                    coverage_percent * system_efficiency_percent / 100, 1
                )
                autarky_origin = (
                    "synthetic_mock"
                    if synthetic
                    else "sfml_stats_loss_adjusted_forecast"
                )
            else:
                expected_autarky_percent = None
                autarky_origin = "unavailable"
            day_summary["expected_autarky_percent"] = expected_autarky_percent
            day_summary["expected_autarky_origin"] = autarky_origin

    def _attach_coverage_stack(
        self,
        days: list[dict[str, Any]],
        components: dict[str, Any],
        *,
        synthetic: bool = False,
    ) -> None:
        """Expose how household demand is covered, with explicit origins."""
        battery_present = bool(components.get("battery"))
        for day in days:
            day_summary = _mapping(day.get("summary"))
            demand_kwh = _number(
                day_summary.get("demand_kwh"), minimum=0, maximum=100000
            )
            direct_pv_covered_kwh = _number(
                day_summary.get("covered_kwh"), minimum=0, maximum=100000
            )
            battery_to_house_kwh = (
                _number(
                    day_summary.get("battery_to_house_kwh"), minimum=0, maximum=100000
                )
                if battery_present
                else 0.0
            )
            expected_grid_import_kwh = _number(
                day_summary.get("expected_grid_import_kwh"), minimum=0, maximum=100000
            )
            pv_after_direct_kwh = _number(
                day_summary.get("pv_potential_after_direct_kwh"),
                minimum=0,
                maximum=100000,
            )
            battery_observed_hours = int(day_summary.get("battery_observed_hours") or 0)
            battery_forecast_hours = int(day_summary.get("battery_forecast_hours") or 0)
            if synthetic:
                pv_origin_label = "Synthetische Demo-Prognose"
                battery_origin_label = "Synthetisches Demo-Profil"
                grid_origin_label = "Restbedarf in der Demo"
            else:
                pv_origin_label = (
                    "Solarprognose für den Kalendertag"
                    if day.get("relative_day", 0) > 0
                    else "Solarprognose, stündlich mit dem Tagesbedarf verschnitten"
                )
                if not battery_present:
                    battery_origin_label = "Kein Speicher im System"
                elif battery_observed_hours and battery_forecast_hours:
                    battery_origin_label = f"{battery_observed_hours} gemessene Stunden, {battery_forecast_hours} aus dem 90-Tage-Median"
                elif battery_observed_hours:
                    battery_origin_label = "Gemessene STATS-Stunden"
                elif battery_forecast_hours:
                    battery_origin_label = (
                        "90-Tage-Median für denselben Wochentag und dieselbe Uhrzeit"
                    )
                else:
                    battery_origin_label = "Keine belastbare Akku-Abgabe"
                grid_origin_label = "Restbedarf nach PV direkt und Akku → Haus"
            autarky_origin = str(day_summary.get("expected_autarky_origin") or "")
            if autarky_origin == "stats_loss_adjusted_actual":
                stats_origin_label = "STATS-Tagesbilanz, verlustbereinigt"
            elif autarky_origin == "sfml_stats_loss_adjusted_forecast":
                stats_origin_label = "PV-Direktdeckung nach Systemwirkungsgrad"
            elif autarky_origin == "synthetic_mock":
                stats_origin_label = "Synthetische Demo"
            else:
                stats_origin_label = "Noch keine belastbare Autarkie"
            rows = []

            def add_row(
                row_id: str, label: str, kwh: float | None, origin: str, tone: str
            ) -> None:
                if kwh is None:
                    return
                coverage_share_percent = (
                    round(max(0.0, min(100.0, kwh / demand_kwh * 100)), 1)
                    if demand_kwh
                    else None
                )
                rows.append(
                    {
                        "id": row_id,
                        "label": label,
                        "kwh": round(kwh, 2),
                        "percent": coverage_share_percent,
                        "origin": origin,
                        "tone": tone,
                    }
                )

            add_row(
                "pv_direct",
                "PV direkt ins Haus",
                direct_pv_covered_kwh,
                pv_origin_label,
                "solar",
            )
            if battery_present:
                add_row(
                    "battery",
                    "Akku → Haus",
                    battery_to_house_kwh,
                    battery_origin_label,
                    "battery",
                )
            add_row(
                "grid", "Netzbezug", expected_grid_import_kwh, grid_origin_label, "grid"
            )
            coverage_parts = []
            if direct_pv_covered_kwh is not None:
                coverage_parts.append(
                    f"{direct_pv_covered_kwh:.1f} kWh direkt von der PV"
                )
            if battery_present and battery_to_house_kwh is not None:
                coverage_parts.append(f"{battery_to_house_kwh:.1f} kWh vom Akku")
            if expected_grid_import_kwh is not None:
                coverage_parts.append(f"{expected_grid_import_kwh:.1f} kWh vom Netz")
            if coverage_parts and demand_kwh is not None:
                coverage_description = (
                    f"Vom Bedarf {demand_kwh:.1f} kWh kommen "
                    + ", ".join(coverage_parts)
                    + "."
                )
            elif coverage_parts:
                coverage_description = "Deckung: " + ", ".join(coverage_parts) + "."
            else:
                coverage_description = (
                    "Für diesen Tag fehlt noch eine belastbare Deckungsprognose."
                )
            day_summary["coverage"] = rows
            day_summary["coverage_story"] = coverage_description
            day_summary["coverage_surplus_kwh"] = pv_after_direct_kwh
            day_summary["autarky_origin_label"] = stats_origin_label
            continue

    def _plan_status(self, actor: dict[str, Any]) -> tuple[str, str, str]:
        desired = actor.get("desired")
        actor_id = str(actor.get("id") or "")
        if (
            self.mode in {"confirm", "automatic"}
            and desired is True
            and (not actor.get("enabled"))
        ):
            return ("enable", "Freigabe nötig", "approval")
        if desired is True:
            return ("start", _PLAN_ON.get(actor_id, "Einschalten empfohlen"), "start")
        if desired is False:
            return ("stop", _PLAN_OFF.get(actor_id, "Jetzt nicht einschalten"), "stop")
        return ("hold", "Halten", "waiting")

    def _apply_schedule(self, item: dict[str, Any]) -> None:
        schedule_label = str(
            item.get("when_range") or item.get("when_label") or "Jetzt"
        )
        if schedule_label.startswith("Ab ") or schedule_label == "Jetzt":
            schedule_label = str(item.get("when_range") or schedule_label)
        hour = _upcoming_window_hour(
            item.get("when_start"), _utcnow().astimezone(self._local_timezone())
        )
        reason_code = str(item.get("reason") or "")
        desired = item.get("desired")
        item["later_hour"] = hour
        if desired is True:
            item["when_heading"] = "Jetzt"
            item["when_label"] = "Jetzt"
            item["when_range"] = schedule_label if hour is not None else ""
            item["later_hint"] = (
                f"Günstigerer Strom ab {hour:02d} Uhr."
                if reason_code == "charge_now_avoids_costlier_later_import"
                and hour is not None
                else ""
            )
            return
        if (
            desired is False
            and reason_code == "later_grid_import_is_cheaper"
            and (hour is not None)
        ):
            item["when_heading"] = "Nächstes Fenster"
            item["when_label"] = f"Ab {hour:02d} Uhr"
            item["when_range"] = schedule_label
            item["later_hint"] = ""
            return
        item["when_heading"] = "Jetzt"
        item["when_label"] = "Jetzt"
        item["when_range"] = ""
        item["later_hint"] = ""
        item["later_hour"] = None

    def _action_plan(
        self, actors: list[dict[str, Any]], windows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        windows_by_actor = {
            str(forecast_window.get("actor_id")): forecast_window
            for forecast_window in windows
            if forecast_window.get("actor_id")
        }

        def readiness(actor: dict[str, Any]) -> int:
            if actor.get("ready") and actor.get("enabled"):
                return 0
            if actor.get("ready"):
                return 1
            return 2

        ready_actors = [
            actor
            for actor in actors
            if actor.get("present") is True
            if actor.get("configured") is True
        ]
        ready_actors.sort(
            key=lambda actor: (
                readiness(actor),
                _ACTOR_PLAN_PRIORITY.get(str(actor.get("id")), 9),
            )
        )
        action_plan = []
        for actor in ready_actors[:4]:
            status_code, status_text, action_label = self._plan_status(actor)
            actor_id = str(actor.get("id"))
            scheduled_window = windows_by_actor.get(actor_id)
            window_start = (
                scheduled_window.get("when_start") if scheduled_window else None
            )
            window_end = scheduled_window.get("when_end") if scheduled_window else None
            when_label = (
                str(scheduled_window.get("when_label"))
                if scheduled_window and scheduled_window.get("when_label")
                else "Jetzt"
            )
            confidence_level = (
                "high"
                if actor.get("ready") and actor.get("source_fresh")
                else "medium" if actor.get("ready") else "low"
            )
            item = {
                "id": f"plan-{actor_id}",
                "device": str(actor.get("label") or actor_id),
                "action": status_code,
                "status": status_text,
                "tone": action_label,
                "when_start": window_start,
                "when_end": window_end,
                "when_label": when_label,
                "why": _humanize_reason(actor.get("reason")),
                "reason": str(actor.get("reason") or ""),
                "detail": str(actor.get("detail") or ""),
                "confidence": confidence_level,
                "actor_id": actor_id,
                "ready": bool(actor.get("ready")),
                "enabled": bool(actor.get("enabled")),
                "desired": actor.get("desired"),
                "confirm_token": actor.get("confirm_token"),
                "source": str(actor.get("source") or ""),
            }
            self._apply_schedule(item)
            action_plan.append(item)
            continue
        return action_plan

    def _window_runs(
        self,
        hours: list[dict[str, Any]],
        *,
        day_date: str,
        kind: str,
        label: str,
        detail_builder,
        predicate,
        actor_id: str | None = None,
        min_hours: int = 1,
        priority: int = 50,
    ) -> list[dict[str, Any]]:
        run_groups = []
        run_stop = None
        flush_last_hour = None
        run_values = []

        def flush() -> None:
            nonlocal run_stop, flush_last_hour, run_start, flush_first_hour, run_values, run_count
            if run_stop is None or flush_last_hour is None or run_start is None:
                return None
            if run_count < min_hours:
                run_stop = None
                flush_last_hour = None
                run_start = None
                flush_first_hour = None
                run_values = []
                run_count = 0
                return
            window_energy_sum = round(sum(run_values), 3) if run_values else None
            run_groups.append(
                {
                    "id": f"{kind}-{run_start}-{run_stop:02d}",
                    "kind": kind,
                    "label": label,
                    "detail": detail_builder(
                        run_stop, flush_last_hour, window_energy_sum
                    ),
                    "when_start": f"{run_start}T{run_stop:02d}:00:00",
                    "when_end": f"{flush_first_hour or run_start}T{flush_last_hour:02d}:59:59",
                    "when_label": _hour_label(run_stop, flush_last_hour),
                    "start_hour": run_stop,
                    "end_hour": flush_last_hour,
                    "value_kwh": window_energy_sum,
                    "actor_id": actor_id,
                    "priority": priority,
                }
            )
            run_stop = None
            flush_last_hour = None
            run_start = None
            flush_first_hour = None
            run_values = []
            run_count = 0

        run_start = None
        flush_first_hour = None
        run_count = 0
        for hour_point in hours:
            hour = hour_point.get("hour")
            if isinstance(hour, int):
                matches_window, predicate_value = predicate(hour_point)
                if matches_window:
                    if run_stop is None:
                        run_stop = hour
                        run_start = str(hour_point.get("_day_date") or day_date)
                    flush_last_hour = hour
                    flush_first_hour = str(hour_point.get("_day_date") or day_date)
                    run_count += 1
                    if predicate_value is not None:
                        run_values.append(float(predicate_value))
                else:
                    flush()
        flush()
        return run_groups

    def _horizon_hours(
        self, days: list[dict[str, Any]], *, hours_ahead: int = _HORIZON_HOURS
    ) -> list[dict[str, Any]]:
        if not days:
            return []
        now_local = _utcnow().astimezone(self._local_timezone())
        current_hour = now_local.hour
        hour_slots = []
        for day in days:
            date_str = str(day.get("date") or "")
            forecast_hours = (
                day.get("hours") if isinstance(day.get("hours"), list) else []
            )
            relative_day = day.get("relative_day")
            for hour_point in forecast_hours:
                hour = hour_point.get("hour")
                if not isinstance(hour, int):
                    continue
                if relative_day == 0:
                    if hour < current_hour:
                        continue
                horizon_point = dict(hour_point)
                horizon_point["_day_date"] = date_str
                hour_slots.append(horizon_point)
                if not len(hour_slots) >= hours_ahead:
                    continue
                return hour_slots
        return hour_slots

    def _forecast_windows(
        self,
        days: list[dict[str, Any]],
        components: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        forecast_hours = self._horizon_hours(days)
        first_day = days[0] if days else {}
        date_str = (
            str(first_day.get("date") or forecast_hours[0].get("_day_date") or "")
            if forecast_hours
            else ""
        )
        if not forecast_hours or not date_str:
            return []
        price_values = [
            float(hour_point["price_ct_kwh"])
            for hour_point in forecast_hours
            if hour_point.get("price_ct_kwh") is not None
        ]
        median_price_ct_kwh = (
            sorted(price_values)[len(price_values) // 2] if price_values else None
        )
        price_spread_ratio = _price_spread_ratio(price_values)
        is_flat_tariff = (
            price_spread_ratio is not None and price_spread_ratio <= _FLAT_TARIFF_SPREAD
        )
        forecast_windows = []

        def surplus_predicate(point: dict[str, Any]) -> tuple[bool, float | None]:
            surplus_kwh = _dispatch_surplus_kwh(point, components)
            if surplus_kwh is not None and surplus_kwh >= 0.25:
                return (True, surplus_kwh)
            return (False, None)

        forecast_windows.extend(
            self._window_runs(
                forecast_hours,
                day_date=date_str,
                kind="surplus",
                label="PV-Überschuss",
                detail_builder=lambda start, end, total: (
                    f"Nach Haus, Wärme und Akku bleiben etwa {total:.1f} kWh zwischen {_hour_label(start, end)}."
                    if total is not None
                    else f"Überschussfenster {_hour_label(start, end)}."
                ),
                predicate=surplus_predicate,
                actor_id="surplus_load",
                min_hours=2,
                priority=10,
            )
        )
        if is_flat_tariff:
            forecast_windows.append(
                {
                    "id": f"fixed-tariff-{date_str}",
                    "kind": "fixed_tariff",
                    "label": "Festtarif",
                    "detail": "Der Strompreis ist über den Tag praktisch konstant. Es gibt keine günstigen oder teuren Preisfenster.",
                    "when_start": None,
                    "when_end": None,
                    "when_label": "24–36 h",
                    "start_hour": None,
                    "end_hour": None,
                    "value_kwh": None,
                    "actor_id": None,
                    "priority": 40,
                }
            )
        else:

            def expensive_predicate(point: dict[str, Any]) -> tuple[bool, float | None]:
                expected_grid_import_kwh = _number(
                    point.get("expected_grid_import_kwh"), minimum=0, maximum=1000
                )
                price_ct_kwh = _number(
                    point.get("price_ct_kwh"), minimum=-1000, maximum=5000
                )
                if (
                    expected_grid_import_kwh is not None
                    and expected_grid_import_kwh >= 0.2
                    and (price_ct_kwh is not None)
                    and (median_price_ct_kwh is not None)
                    and (price_ct_kwh >= median_price_ct_kwh * 1.12)
                ):
                    return (True, expected_grid_import_kwh)
                return (False, None)

            forecast_windows.extend(
                self._window_runs(
                    forecast_hours,
                    day_date=date_str,
                    kind="expensive_grid",
                    label="Teurer Netzbezug",
                    detail_builder=lambda start, end, total: (
                        f"Erwarteter Netzbezug ca. {total:.1f} kWh in {_hour_label(start, end)}."
                        if total is not None
                        else f"Teures Netzfenster {_hour_label(start, end)}."
                    ),
                    predicate=expensive_predicate,
                    min_hours=1,
                    priority=20,
                )
            )
            if components.get("battery"):

                def cheap_predicate(point: dict[str, Any]) -> tuple[bool, float | None]:
                    price_ct_kwh = _number(
                        point.get("price_ct_kwh"), minimum=-1000, maximum=5000
                    )
                    if (
                        price_ct_kwh is not None
                        and median_price_ct_kwh is not None
                        and (price_ct_kwh <= median_price_ct_kwh * 0.88)
                    ):
                        return (True, None)
                    return (False, None)

                forecast_windows.extend(
                    self._window_runs(
                        forecast_hours,
                        day_date=date_str,
                        kind="cheap_grid",
                        label="Günstiges Laden",
                        detail_builder=lambda start, end, _total: f"Niedrige Strompreise {_hour_label(start, end)} – sinnvoll für geplante Netzladung.",
                        predicate=cheap_predicate,
                        actor_id="battery_grid_charge",
                        min_hours=2,
                        priority=30,
                    )
                )
        forecast_windows.sort(
            key=lambda item: (
                int(item.get("priority") if item.get("priority") is not None else 99),
                (
                    item.get("start_hour")
                    if isinstance(item.get("start_hour"), int)
                    else 99
                ),
            )
        )
        heat_pump_window = (
            self._eai_optimization_window(snapshot, date_str)
            if components.get("heat_pump")
            else None
        )
        if heat_pump_window:
            forecast_windows.insert(0, heat_pump_window)
        return forecast_windows[:5]

    def _today_brief(
        self, day_summary: dict[str, Any], windows: list[dict[str, Any]]
    ) -> dict[str, Any]:
        next_window = windows[0] if windows else None
        coverage_percent = day_summary.get("coverage")
        return (
            (
                {
                    "expected_grid_import_kwh": day_summary.get(
                        "expected_grid_import_kwh"
                    ),
                    "expected_grid_cost_eur": day_summary.get("expected_grid_cost_eur"),
                    "pv_kwh": day_summary.get("pv_kwh"),
                    "demand_kwh": day_summary.get("demand_kwh"),
                    "covered_kwh": day_summary.get("covered_kwh"),
                    "coverage_percent": day_summary.get("coverage_percent"),
                    "battery_to_house_kwh": day_summary.get("battery_to_house_kwh"),
                    "pv_potential_after_direct_kwh": day_summary.get(
                        "pv_potential_after_direct_kwh"
                    ),
                    "expected_autarky_percent": day_summary.get(
                        "expected_autarky_percent"
                    ),
                    "autarky_origin_label": day_summary.get("autarky_origin_label"),
                    "coverage": (
                        coverage_percent if isinstance(coverage_percent, list) else []
                    ),
                    "coverage_story": str(day_summary.get("coverage_story") or ""),
                    "coverage_surplus_kwh": day_summary.get("coverage_surplus_kwh"),
                    "household_kwh": day_summary.get("household_kwh"),
                    "heat_pump_kwh": day_summary.get("heat_pump_kwh"),
                    "wallbox_kwh": day_summary.get("wallbox_kwh"),
                    "next_window_id": next_window.get("id") if next_window else None,
                    "next_window_label": (
                        next_window.get("label") if next_window else None
                    ),
                    "next_window_detail": (
                        next_window.get("detail") if next_window else None
                    ),
                    "next_window_when": (
                        next_window.get("when_label") if next_window else None
                    ),
                }
                if True
                else None
            )
            if True
            else None
        )

    def _hour_points_from(
        self, hours: list[dict[str, Any]], start_hour: int
    ) -> list[dict[str, Any]]:
        return [
            hour_point
            for hour_point in hours
            if isinstance(hour_point.get("hour"), int)
            if hour_point["hour"] >= start_hour
        ]

    def _peak_grid_hour(self, hours: list[dict[str, Any]]) -> dict[str, Any] | None:
        valid_hours = [
            hour_point
            for hour_point in hours
            if _number(hour_point.get("expected_grid_import_kwh"), minimum=0)
            is not None
        ]
        if not valid_hours:
            return
        peak_grid_point = max(
            valid_hours,
            key=lambda point: float(point.get("expected_grid_import_kwh") or 0),
        )
        hour = int(peak_grid_point["hour"])
        expected_grid_import_kwh = _number(
            peak_grid_point.get("expected_grid_import_kwh"), minimum=0
        )
        price_ct_kwh = _number(
            peak_grid_point.get("price_ct_kwh"), minimum=-1000, maximum=5000
        )
        expected_grid_cost_eur = _number(
            peak_grid_point.get("expected_grid_cost_eur"), minimum=0, maximum=1000
        )
        return {
            "hour": hour,
            "when_label": _hour_label(hour, hour),
            "grid_kwh": expected_grid_import_kwh,
            "price_ct_kwh": price_ct_kwh,
            "cost_eur": expected_grid_cost_eur,
            "origin": "Nächste Stunde mit dem höchsten erwarteten Netzbezug",
        }

    def _best_charge_hour(self, hours: list[dict[str, Any]]) -> dict[str, Any] | None:
        valid_hours = [
            hour_point
            for hour_point in hours
            if _number(hour_point.get("price_ct_kwh"), minimum=-1000, maximum=5000)
            is not None
        ]
        if not valid_hours:
            return
        cheapest_hour = min(valid_hours, key=lambda point: float(point["price_ct_kwh"]))
        hour = int(cheapest_hour["hour"])
        return {
            "hour": hour,
            "when_label": _hour_label(hour, hour),
            "price_ct_kwh": _number(
                cheapest_hour.get("price_ct_kwh"), minimum=-1000, maximum=5000
            ),
            "origin": "Günstigste Reststunde im GPM-Preis über 24–36 h",
        }

    def _ems_operations(self, payload: dict[str, Any]) -> dict[str, Any]:
        days = payload.get("days") if isinstance(payload.get("days"), list) else []
        day_summary = _mapping(payload.get("summary"))
        components = _mapping(payload.get("components"))
        first_day = days[0] if days else {}
        today_hours = (
            first_day.get("hours") if isinstance(first_day.get("hours"), list) else []
        )
        planning_hours = self._horizon_hours(days)
        forecast_hours = planning_hours or today_hours
        now_local = _utcnow().astimezone(self._local_timezone())
        current_hour = now_local.hour if first_day.get("relative_day") == 0 else 0
        near_term_hours = planning_hours[:4]
        surplus_state, surplus_updated_at = self._surplus_state()
        surplus_attributes = _mapping(getattr(surplus_state, "attributes", None))
        surplus_power_w = _number(
            surplus_attributes.get("surplus_power_w"), minimum=0, maximum=1000000
        )
        battery_soc_percent = _number(
            day_summary.get("battery_soc_percent"), minimum=0, maximum=100
        )
        battery_capacity_kwh = _number(
            self.config.get(CONF_BATTERY_CAPACITY), minimum=0, maximum=200
        )
        if battery_capacity_kwh is not None and battery_capacity_kwh <= 0:
            battery_capacity_kwh = None
        gpm_data, gpm_updated_at, gpm_fresh = self._gpm()
        requested_grid_charge_kwh = _number(
            gpm_data.get("smart_charging_requested_grid_charge_kwh"),
            minimum=0,
            maximum=200,
        )
        today_pv_kwh = self._available_total(today_hours, "pv_kwh")
        covered_kwh = self._available_total(today_hours, "covered_kwh")
        today_pv_after_direct_kwh = self._available_total(
            today_hours, "pv_potential_after_direct_kwh"
        )
        horizon_pv_kwh = self._available_total(planning_hours, "pv_kwh")
        horizon_demand_kwh = self._available_total(planning_hours, "demand_kwh")
        horizon_surplus_values = [
            _dispatch_surplus_kwh(hour_point, components)
            for hour_point in planning_hours
        ]
        horizon_surplus_kwh = (
            round(
                sum(
                    (
                        surplus_kwh
                        for surplus_kwh in horizon_surplus_values
                        if surplus_kwh is not None
                    )
                ),
                2,
            )
            if any((surplus_kwh is not None for surplus_kwh in horizon_surplus_values))
            else None
        )
        horizon_grid_import_kwh = self._available_total(
            planning_hours, "expected_grid_import_kwh"
        )
        horizon_grid_cost_eur = self._available_total(
            planning_hours, "expected_grid_cost_eur"
        )
        near_term_pv_kwh = self._available_total(near_term_hours, "pv_kwh")
        near_term_demand_kwh = self._available_total(near_term_hours, "demand_kwh")
        near_term_surplus_values = [
            _dispatch_surplus_kwh(hour_point, components)
            for hour_point in near_term_hours
        ]
        near_term_surplus_kwh = (
            round(
                sum(
                    (
                        surplus_kwh
                        for surplus_kwh in near_term_surplus_values
                        if surplus_kwh is not None
                    )
                ),
                2,
            )
            if any(
                (surplus_kwh is not None for surplus_kwh in near_term_surplus_values)
            )
            else None
        )
        near_term_grid_import_kwh = self._available_total(
            near_term_hours, "expected_grid_import_kwh"
        )
        direct_pv_coverage_percent = (
            round(covered_kwh / today_pv_kwh * 100, 1)
            if today_pv_kwh not in {None, 0} and covered_kwh is not None
            else None
        )
        battery_headroom_kwh = (
            round(
                battery_capacity_kwh * max(0.0, 100.0 - battery_soc_percent) / 100.0, 2
            )
            if battery_capacity_kwh is not None and battery_soc_percent is not None
            else None
        )
        grid_import_avoidance_kwh = None
        if horizon_surplus_kwh is not None and horizon_grid_import_kwh is not None:
            grid_import_avoidance_kwh = round(
                min(horizon_surplus_kwh, horizon_grid_import_kwh), 2
            )
        kpi_rows = []

        def add_kpi(
            kpi_id: str,
            label: str,
            value: float | None,
            *,
            kind: str,
            hint: str,
            origin: str,
        ) -> None:
            if value is None:
                return
            if kind == "energy":
                formatted_value = f"{value:.1f} kWh"
            elif kind == "percent":
                formatted_value = f"{round(value)} %"
            elif kind == "currency":
                formatted_value = f"{value:.2f} €"
            elif kind == "power":
                formatted_value = f"{round(value)} W"
            else:
                formatted_value = str(value)
            kpi_rows.append(
                {
                    "id": kpi_id,
                    "label": label,
                    "value": value,
                    "kind": kind,
                    "display": formatted_value,
                    "hint": hint,
                    "origin": origin,
                }
            )

        add_kpi(
            "self_consumption",
            "Eigenverbrauch",
            direct_pv_coverage_percent,
            kind="percent",
            hint="Anteil der PV, der direkt ins Haus geht",
            origin="PV direkt / PV-Ertrag aus der Solarprognose",
        )
        add_kpi(
            "shift_potential",
            "Verschiebepotenzial",
            today_pv_after_direct_kwh,
            kind="energy",
            hint="PV nach Direktdeckung, noch vor der Speicherreserve",
            origin="PV minus direkten Hausbedarf, vor Speicher",
        )
        add_kpi(
            "remaining_surplus",
            "Restlicher Überschuss",
            horizon_surplus_kwh,
            kind="energy",
            hint="PV nach Haus, Wärme und Akku über den nächsten 24–36 Stunden",
            origin="Allokationsrest der offenen Prognosestunden",
        )
        add_kpi(
            "remaining_grid_cost",
            "Restliche Netzkosten",
            horizon_grid_cost_eur,
            kind="currency",
            hint="Erwartete Kosten für den restlichen Netzbezug",
            origin="Restlicher Netzbezug × GPM-Preis",
        )
        if components.get("battery"):
            add_kpi(
                "battery_headroom",
                "Akku-Freiraum",
                battery_headroom_kwh,
                kind="energy",
                hint="Noch aufnehmbare Energie bis 100 % SoC",
                origin="Aktueller SoC und konfigurierte Kapazität",
            )
        add_kpi(
            "live_surplus",
            "Aktueller Überschuss",
            surplus_power_w,
            kind="power",
            hint="Stabiles S-Plus-Signal in diesem Moment",
            origin="STATS Überschuss-Binärsensor",
        )
        if requested_grid_charge_kwh is not None and requested_grid_charge_kwh > 0:
            add_kpi(
                "planned_grid_charge",
                "Geplante Netzladung",
                requested_grid_charge_kwh,
                kind="energy",
                hint="Vom Smart-Charging-Plan angeforderte Menge",
                origin="GPM Smart-Charging-Entscheidung",
            )
        peak_grid_summary = self._peak_grid_hour(planning_hours or forecast_hours)
        best_charge_hour_summary = self._best_charge_hour(
            planning_hours or forecast_hours
        )
        near_term_start_hour = (
            int(near_term_hours[0]["hour"]) if near_term_hours else current_hour
        )
        near_term_end_hour = (
            int(near_term_hours[-1]["hour"]) if near_term_hours else current_hour
        )
        price_values = [
            float(hour_point["price_ct_kwh"])
            for hour_point in planning_hours
            if hour_point.get("price_ct_kwh") is not None
        ]
        price_spread_ratio = _price_spread_ratio(price_values)
        is_flat_tariff = (
            price_spread_ratio is not None and price_spread_ratio <= _FLAT_TARIFF_SPREAD
        )
        dispatch_explanation = (
            f"Der restliche PV-Überschuss nach Speicher kann noch bis zu {grid_import_avoidance_kwh:.1f} kWh Netzbezug ersetzen."
            if grid_import_avoidance_kwh
            else "Kein belastbares Ersatzpotenzial für den restlichen Tag."
        )
        if is_flat_tariff:
            dispatch_explanation = (
                "Festtarif: keine Preisspreizung im Horizont. " + dispatch_explanation
            )
        return {
            "kpis": kpi_rows,
            "horizon": {
                "label": "Nächste Stunden",
                "when_label": (
                    _hour_label(near_term_start_hour, near_term_end_hour)
                    if near_term_hours
                    else "–"
                ),
                "pv_kwh": near_term_pv_kwh,
                "demand_kwh": near_term_demand_kwh,
                "surplus_kwh": near_term_surplus_kwh,
                "grid_kwh": near_term_grid_import_kwh,
                "remaining_pv_kwh": horizon_pv_kwh,
                "remaining_demand_kwh": horizon_demand_kwh,
                "remaining_grid_kwh": horizon_grid_import_kwh,
            },
            "peak": peak_grid_summary,
            "best_charge": best_charge_hour_summary,
            "tariff": {
                "kind": "fixed" if is_flat_tariff else "dynamic",
                "detail": (
                    "Festtarif: günstige und teure Preisfenster sind nicht definiert."
                    if is_flat_tariff
                    else "Dynamischer Preis: Fenster relativ zum Median."
                ),
            },
            "opportunity": {
                "offset_kwh": grid_import_avoidance_kwh,
                "detail": dispatch_explanation,
            },
        }

    def _eai_optimization_window(
        self, snapshot: dict[str, Any] | None, day_date: str
    ) -> dict[str, Any] | None:
        snapshot_sections = _mapping((snapshot or {}).get("sections"))
        forecast_section = _mapping(snapshot_sections.get("forecast"))
        energy_section = _mapping(snapshot_sections.get("energy"))
        optimization = _mapping(
            forecast_section.get("optimization") or energy_section.get("optimization")
        )
        if optimization.get("available") is not True:
            return
        recommended_start = _parse_time(optimization.get("start"))
        if recommended_start is None:
            return
        optimization_start_local = recommended_start.astimezone(self._local_timezone())
        if optimization_start_local.date().isoformat() != day_date:
            return
        duration_minutes = _number(
            optimization.get("duration_minutes"), minimum=15, maximum=1440
        )
        duration_hours = max(1, int(round((duration_minutes or 60) / 60)))
        end_hour = min(23, optimization_start_local.hour + duration_hours - 1)
        pv_after_direct_kwh = _number(
            optimization.get("pv_surplus_kwh"), minimum=0, maximum=1000
        )
        optimization_explanation = _mapping(optimization.get("explanation"))
        recommendation_text = str(
            optimization.get("recommendation")
            or optimization_explanation.get("summary")
            or "PV-Fenster für Wärme erkannt."
        )
        return {
            "id": f"eai-heat-{day_date}-{optimization_start_local.hour:02d}",
            "kind": "eai_heat_window",
            "label": "Wärmespeicher im PV-Fenster",
            "detail": recommendation_text,
            "when_start": f"{day_date}T{optimization_start_local.hour:02d}:00:00",
            "when_end": f"{day_date}T{end_hour:02d}:59:59",
            "when_label": _hour_label(optimization_start_local.hour, end_hour),
            "start_hour": optimization_start_local.hour,
            "end_hour": end_hour,
            "value_kwh": pv_after_direct_kwh,
            "actor_id": "heat_pump_boost",
            "priority": 5,
            "confidence_percent": _number(
                optimization.get("confidence_percent"), minimum=0, maximum=100
            ),
            "origin": str(
                optimization.get("origin") or "sfml_pv_forecast_and_eai_load_model"
            ),
        }

    @staticmethod
    def _roundtrip_percent(value: Any) -> float | None:
        efficiency_percent = _number(value, minimum=0, maximum=100)
        if efficiency_percent is None:
            return
        if efficiency_percent <= 1:
            return round(efficiency_percent * 100, 1)
        return round(efficiency_percent, 1)

    def _allocation_rows(
        self,
        energy: dict[str, Any],
        day_summary: dict[str, Any],
        components: dict[str, Any],
    ) -> list[dict[str, Any]]:
        allocation_key_aliases = {
            "battery_pv_reserved_kwh": ("battery_pv_reserve_kwh",),
            "residual_pv_kwh": ("wallbox_pv_available_kwh",),
            "unallocated_or_export_kwh": ("expected_grid_export_kwh",),
        }
        allocation_rows = []
        for (
            energy_field,
            allocation_label,
            allocation_detail,
            allocation_source,
        ) in _ALLOCATION_ROWS:
            if energy_field == "heat_pump_pv_kwh":
                if not components.get("heat_pump"):
                    continue
            if energy_field == "battery_pv_reserved_kwh":
                if not components.get("battery"):
                    continue
            if energy_field == "residual_pv_kwh":
                allocation_label = (
                    "Wallbox / Rest" if components.get("wallbox") else "Überschuss"
                )
                allocation_source = (
                    "PV nach Haus, Wärme und Akku"
                    if components.get("wallbox")
                    else "PV nach den vorhandenen Verbrauchern"
                )
            if energy_field == "unallocated_or_export_kwh":
                if not components.get("wallbox"):
                    continue
            source_entry = _number(energy.get(energy_field), minimum=0, maximum=100000)
            if source_entry is None:
                for fallback_energy_key in allocation_key_aliases.get(energy_field, ()):
                    source_entry = _number(
                        energy.get(fallback_energy_key), minimum=0, maximum=100000
                    )
                    if not source_entry is not None:
                        continue
                    break
            if source_entry is None:
                source_entry = _number(
                    day_summary.get(energy_field), minimum=0, maximum=100000
                )
            if source_entry is None:
                continue
            allocation_rows.append(
                {
                    "id": energy_field,
                    "label": allocation_label,
                    "kwh": source_entry,
                    "tone": allocation_detail,
                    "origin": allocation_source,
                }
            )
            continue
        total_allocated_kwh = sum(
            (float(allocation_row["kwh"]) for allocation_row in allocation_rows)
        )
        for allocation_row in allocation_rows:
            allocation_row["percent"] = (
                round(float(allocation_row["kwh"]) / total_allocated_kwh * 100, 1)
                if total_allocated_kwh > 0
                else None
            )
        return allocation_rows

    def _intelligence(
        self,
        payload: dict[str, Any],
        *,
        snapshot: dict[str, Any] | None = None,
        gpm: dict[str, Any] | None = None,
        mobility: dict[str, Any] | None = None,
        building: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if False:
            __assert_armored__ = None
        snapshot_sections = _mapping((snapshot or {}).get("sections"))
        forecast_section = _mapping(snapshot_sections.get("forecast"))
        energy_section = _mapping(snapshot_sections.get("energy"))
        overview_section = _mapping(snapshot_sections.get("overview"))
        building = _mapping(building or snapshot_sections.get("building"))
        mobility = _mapping(mobility or snapshot_sections.get("mobility"))
        gpm = _mapping(gpm)
        components = _mapping(payload.get("components"))
        days = payload.get("days") if isinstance(payload.get("days"), list) else []
        combined_summary = {
            **_mapping(payload.get("summary")),
            **(_mapping(days[0].get("summary")) if days else {}),
        }
        forecast_hours = days[0].get("hours") if days else []
        forecast_hours = forecast_hours if isinstance(forecast_hours, list) else []
        optimization = _mapping(
            forecast_section.get("optimization") or energy_section.get("optimization")
        )
        optimization_explanation = _mapping(optimization.get("explanation"))
        allocation_provenance = _mapping(energy_section.get("allocation_provenance"))
        briefing = _mapping(overview_section.get("briefing"))
        evidence_lines = []
        if components.get("heat_pump"):
            for evidence_or_priority in optimization_explanation.get("evidence") or []:
                evidence_text = str(evidence_or_priority).strip()
                if (
                    evidence_text
                    and evidence_text not in evidence_lines
                    and _why_fits_components(evidence_text, components)
                ):
                    evidence_lines.append(evidence_text)
            if optimization_explanation.get("summary"):
                explanation_summary = str(optimization_explanation["summary"]).strip()
                if (
                    explanation_summary
                    and explanation_summary not in evidence_lines
                    and _why_fits_components(explanation_summary, components)
                ):
                    evidence_lines.insert(0, explanation_summary)
        allocation_rows = self._allocation_rows(
            energy_section, combined_summary, components
        )
        confidence_values = [
            float(hour_point["confidence_percent"])
            for hour_point in forecast_hours
            if hour_point.get("confidence_percent") is not None
        ]
        uncertainty_values = [
            float(hour_point["uncertainty_percent"])
            for hour_point in forecast_hours
            if hour_point.get("uncertainty_percent") is not None
        ]
        confidence_percent = (
            round(sum(confidence_values) / len(confidence_values))
            if confidence_values
            else _number(
                combined_summary.get("forecast_confidence_percent"),
                minimum=0,
                maximum=100,
            )
        )
        uncertainty_percent = (
            round(sum(uncertainty_values) / len(uncertainty_values))
            if uncertainty_values
            else None
        )
        if confidence_percent is None:
            confidence_percent = _number(
                energy_section.get("energy_context_quality_percent"),
                minimum=0,
                maximum=100,
            )
        storage_cost_ct_kwh = _number(
            gpm.get("smart_charging_effective_storage_cost_ct_kwh"),
            minimum=-1000,
            maximum=5000,
        )
        future_price_ct_kwh = _number(
            gpm.get("smart_charging_compared_future_price_ct_kwh"),
            minimum=-1000,
            maximum=5000,
        )
        system_efficiency_percent = self._roundtrip_percent(
            gpm.get("smart_charging_effective_roundtrip_efficiency")
        )
        battery_economics = None
        battery_actor = next(
            (
                actor
                for actor in (
                    payload.get("actors")
                    if isinstance(payload.get("actors"), list)
                    else []
                )
                if isinstance(actor, dict) and actor.get("id") == "battery_grid_charge"
            ),
            {},
        )
        if (
            components.get("battery")
            and storage_cost_ct_kwh is not None
            and (future_price_ct_kwh is not None)
        ):
            storage_saving_ct_kwh = round(future_price_ct_kwh - storage_cost_ct_kwh, 2)
            desired = battery_actor.get("desired")
            is_storage_charge_economic = (
                desired is True
                if isinstance(desired, bool)
                else storage_saving_ct_kwh > 0
            )
            recommendation_text = _battery_dev_text(
                reason=str(battery_actor.get("reason") or ""),
                storage_cost=storage_cost_ct_kwh,
                future_price=future_price_ct_kwh,
                efficiency=system_efficiency_percent,
            )
            battery_economics = {
                "worthwhile": is_storage_charge_economic,
                "storage_cost_ct_kwh": storage_cost_ct_kwh,
                "future_price_ct_kwh": future_price_ct_kwh,
                "spread_ct_kwh": storage_saving_ct_kwh,
                "roundtrip_efficiency_percent": system_efficiency_percent,
                "detail": recommendation_text,
            }
            evidence_lines.append(recommendation_text)
        mobility_details = None
        if components.get("wallbox"):
            mobility_cost_advantage_eur = _number(
                mobility.get("estimated_cost_advantage_eur"),
                minimum=-1000,
                maximum=1000,
            )
            departure_readiness_percent = _number(
                mobility.get("departure_readiness_percent"), minimum=0, maximum=100
            )
            required_ev_energy_kwh = _number(
                mobility.get("required_energy_kwh"), minimum=0, maximum=200
            )
            if (
                mobility.get("available") is True
                or mobility_cost_advantage_eur is not None
                or departure_readiness_percent is not None
            ):
                recommended_start = _parse_time(mobility.get("recommended_start"))
                recommended_end = _parse_time(mobility.get("recommended_end"))
                when_label = None
                if recommended_start is not None:
                    charge_start_local = recommended_start.astimezone(
                        self._local_timezone()
                    )
                    charge_end_local = (
                        recommended_end.astimezone(self._local_timezone())
                        if recommended_end is not None
                        else charge_start_local
                    )
                    when_label = _hour_label(
                        charge_start_local.hour, charge_end_local.hour
                    )
                mobility_explanations = []
                if mobility_cost_advantage_eur is not None:
                    mobility_explanations.append(
                        f"Kostenvorteil {mobility_cost_advantage_eur:.2f} € gegenüber Sofortladen"
                    )
                if departure_readiness_percent is not None:
                    mobility_explanations.append(
                        f"Abfahrtbereitschaft {round(departure_readiness_percent)} %"
                    )
                if required_ev_energy_kwh is not None:
                    mobility_explanations.append(
                        f"benötigt {required_ev_energy_kwh:.1f} kWh"
                    )
                mobility_recommendation = (
                    " · ".join(mobility_explanations)
                    if mobility_explanations
                    else str(
                        mobility.get("recommendation_explanation")
                        or "Ladeplan aus PV-Rest und Abfahrt."
                    )
                )
                mobility_details = {
                    "available": True,
                    "cost_advantage_eur": mobility_cost_advantage_eur,
                    "departure_readiness_percent": departure_readiness_percent,
                    "required_energy_kwh": required_ev_energy_kwh,
                    "departure_time": mobility.get("departure_time"),
                    "recommended_start": mobility.get("recommended_start"),
                    "recommended_end": mobility.get("recommended_end"),
                    "when_label": when_label,
                    "pv_share_percent": _number(
                        mobility.get("expected_pv_share_percent"),
                        minimum=0,
                        maximum=100,
                    ),
                    "detail": mobility_recommendation,
                }
                if mobility_recommendation not in evidence_lines:
                    evidence_lines.append(mobility_recommendation)
        building_details = None
        if components.get("heat_pump"):
            indoor_temperature_c = _number(
                building.get("indoor_c"), minimum=-50, maximum=60
            )
            target_temperature_c = _number(
                building.get("target_c"), minimum=-50, maximum=60
            )
            heat_loss_kw = _number(building.get("heat_loss_kw"), minimum=0, maximum=50)
            thermal_loss_data = _mapping(building.get("thermal_loss"))
            thermal_loss_kwh_24h = _number(
                thermal_loss_data.get("forecast_thermal_loss_kwh_24h"),
                minimum=0,
                maximum=100,
            )
            if (
                indoor_temperature_c is not None
                or heat_loss_kw is not None
                or thermal_loss_kwh_24h is not None
            ):
                building_explanations = []
                if (
                    indoor_temperature_c is not None
                    and target_temperature_c is not None
                ):
                    building_explanations.append(
                        f"Innen {indoor_temperature_c:.1f} °C bei Soll {target_temperature_c:.1f} °C"
                    )
                elif indoor_temperature_c is not None:
                    building_explanations.append(f"Innen {indoor_temperature_c:.1f} °C")
                if heat_loss_kw is not None:
                    building_explanations.append(f"Wärmeverlust {heat_loss_kw:.1f} kW")
                if thermal_loss_kwh_24h is not None:
                    building_explanations.append(
                        f"Speicherverlust {thermal_loss_kwh_24h:.1f} kWh/24h"
                    )
                building_details = {
                    "indoor_c": indoor_temperature_c,
                    "target_c": target_temperature_c,
                    "heat_loss_kw": heat_loss_kw,
                    "thermal_loss_kwh_24h": thermal_loss_kwh_24h,
                    "learning_percent": _number(
                        building.get("learning_progress_percent"),
                        minimum=0,
                        maximum=100,
                    ),
                    "detail": (
                        " · ".join(building_explanations)
                        if building_explanations
                        else ""
                    ),
                }
        optimization_details = None
        if components.get("heat_pump") and optimization:
            recommended_action = str(optimization.get("recommendation") or "").strip()
            recommended_start = _parse_time(optimization.get("start"))
            when_label = None
            if recommended_start is not None:
                optimization_start_local = recommended_start.astimezone(
                    self._local_timezone()
                )
                duration_minutes = _number(
                    optimization.get("duration_minutes"), minimum=15, maximum=1440
                )
                duration_hours = max(1, int(round((duration_minutes or 60) / 60)))
                when_label = _hour_label(
                    optimization_start_local.hour,
                    min(23, optimization_start_local.hour + duration_hours - 1),
                )
            optimization_details = {
                "available": optimization.get("available") is True,
                "recommendation": recommended_action or None,
                "headline": str(
                    optimization_explanation.get("headline")
                    or briefing.get("headline")
                    or ""
                )
                or None,
                "when_label": when_label,
                "pv_surplus_kwh": _number(
                    optimization.get("pv_surplus_kwh"), minimum=0, maximum=1000
                ),
                "confidence_percent": _number(
                    optimization.get("confidence_percent"), minimum=0, maximum=100
                ),
                "origin": str(
                    optimization.get("origin") or "sfml_pv_forecast_and_eai_load_model"
                ),
            }
        allocation_priority = allocation_provenance.get("priority_order")
        if not isinstance(allocation_priority, list):
            allocation_priority = [
                "household_base_load",
                "heat_pump",
                "battery_reserve",
                "wallbox",
                "grid_export",
            ]
        priority_rows = []
        for evidence_or_priority in allocation_priority:
            priority_key = str(evidence_or_priority).strip()
            required_component = _PRIORITY_REQUIRED.get(priority_key)
            if (
                not priority_key
                or required_component is None
                or (not components.get(required_component))
            ):
                continue
            allocation_label = _PRIORITY_LABELS.get(
                priority_key, priority_key.replace("_", " ").strip().capitalize()
            )
            if allocation_label not in priority_rows:
                priority_rows.append(allocation_label)
        timing_rows = []
        for forecast_window in payload.get("forecast_windows") or []:
            if not isinstance(forecast_window, dict):
                continue
            timing_rows.append(
                {
                    "id": forecast_window.get("id"),
                    "title": forecast_window.get("label"),
                    "kind": forecast_window.get("kind"),
                    "when_label": forecast_window.get("when_label"),
                    "detail": forecast_window.get("detail"),
                    "origin": forecast_window.get("origin") or "ems_forecast_window",
                }
            )
        dispatch_origin = ""
        if components.get("heat_pump"):
            dispatch_origin = str((optimization_details or {}).get("headline") or "")
        if not dispatch_origin:
            dispatch_origin = str(
                briefing.get("headline")
                or "Entscheidung aus Prognose, Allokation und Preisen"
            )
        evidence_lines = [
            evidence_or_priority
            for evidence_or_priority in evidence_lines
            if _why_fits_components(evidence_or_priority, components)
        ]
        has_intelligence = bool(
            evidence_lines
            or allocation_rows
            or (optimization_details and optimization_details.get("available"))
            or battery_economics
            or mobility_details
            or building_details
        )
        return {
            "available": has_intelligence,
            "headline": dispatch_origin,
            "why": evidence_lines[:6],
            "allocation": allocation_rows,
            "priority_order": priority_rows,
            "forecast_confidence_percent": (
                int(confidence_percent) if confidence_percent is not None else None
            ),
            "forecast_uncertainty_percent": (
                int(uncertainty_percent) if uncertainty_percent is not None else None
            ),
            "optimization": optimization_details,
            "economics": battery_economics,
            "mobility": mobility_details,
            "comfort": building_details,
            "calendar": timing_rows,
            "origin": "sfml_forecast + stats_context + eai_allocation + gpm_prices",
        }

    def _enrich_plan_with_intelligence(
        self, plan: list[dict[str, Any]], intelligence: dict[str, Any]
    ) -> list[dict[str, Any]]:
        battery_economics = _mapping(intelligence.get("economics"))
        optimization = _mapping(intelligence.get("optimization"))
        mobility_section = _mapping(intelligence.get("mobility"))
        for item in plan:
            actor_id = item.get("actor_id")
            if actor_id == "battery_grid_charge" and battery_economics.get("detail"):
                item["why"] = str(battery_economics["detail"])
            elif actor_id == "heat_pump_boost" and optimization.get("recommendation"):
                item["why"] = str(optimization["recommendation"])
                if optimization.get("when_label"):
                    item["when_label"] = optimization["when_label"]
            elif actor_id == "wallbox" and mobility_section.get("detail"):
                item["why"] = str(mobility_section["detail"])
            if item.get("actor_id") != "heat_pump_boost":
                self._apply_schedule(item)
            if not actor_id == "battery_grid_charge":
                continue
            later_hour = item.get("later_hour")
            item["customer_text"] = _battery_customer_text(
                reason=str(item.get("reason") or ""),
                desired=(
                    item.get("desired")
                    if isinstance(item.get("desired"), bool)
                    else None
                ),
                storage_cost=battery_economics.get("storage_cost_ct_kwh"),
                later_hour=later_hour if isinstance(later_hour, int) else None,
            )
            continue
        return plan

    def _lines_for_battery(
        self, gpm: dict[str, Any] | None, item: dict[str, Any]
    ) -> list[str]:
        from .charging_status import grid_release_view as grid_release_view

        grid_price_data = gpm if isinstance(gpm, dict) else {}
        now_local = datetime.now(self._local_timezone())
        battery_status = grid_release_view(
            grid_price_data, now_local, is_demo=bool(grid_price_data.get("is_demo"))
        )
        current_state = item.get("current")
        if self.mode == "observe":
            is_charging = current_state is True
        else:
            is_charging = item.get("desired") is True and bool(
                battery_status["allowed"]
            )
        smart_charging_mode = grid_price_data.get(
            "smart_charging_mode"
        ) or self.config.get(CONF_SMART_CHARGING_MODE)
        return _battery_status_lines(
            reason=str(item.get("reason") or ""),
            active=is_charging,
            grid_kind=str(battery_status["kind"]),
            mode=str(smart_charging_mode) if smart_charging_mode else None,
            target_soc=grid_price_data.get("smart_charging_target_soc")
            or self.config.get(CONF_TARGET_SOC),
            max_soc=grid_price_data.get("smart_charging_max_soc")
            or self.config.get(CONF_MAX_SOC),
            min_soc=grid_price_data.get("smart_charging_min_soc")
            or self.config.get(CONF_MIN_SOC),
            next_cheap_hour=grid_price_data.get("next_cheap_hour"),
            under_force=bool(battery_status["under_force"]),
            now_hour=now_local.hour,
            observe=self.mode == "observe",
        )

    def _attach_action_fields(
        self,
        payload: dict[str, Any],
        *,
        snapshot: dict[str, Any] | None = None,
        gpm: dict[str, Any] | None = None,
        mobility: dict[str, Any] | None = None,
        building: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        days = payload.get("days") if isinstance(payload.get("days"), list) else []
        actors = (
            payload.get("actors") if isinstance(payload.get("actors"), list) else []
        )
        components = _mapping(payload.get("components"))
        combined_summary = {
            **_mapping(payload.get("summary")),
            **(_mapping(days[0].get("summary")) if days else {}),
        }
        forecast_windows = self._forecast_windows(days, components, snapshot)
        action_plan = self._action_plan(actors, forecast_windows)
        today_briefing = self._today_brief(combined_summary, forecast_windows)
        payload["schema_version"] = 3
        payload["action_plan"] = action_plan
        payload["forecast_windows"] = forecast_windows
        payload["today_brief"] = today_briefing
        payload["operations"] = self._ems_operations(payload)
        intelligence = self._intelligence(
            payload, snapshot=snapshot, gpm=gpm, mobility=mobility, building=building
        )
        payload["intelligence"] = intelligence
        payload["action_plan"] = self._enrich_plan_with_intelligence(
            action_plan, intelligence
        )
        if payload["action_plan"]:
            first_plan_item = payload["action_plan"][0]
            status_text = str(first_plan_item.get("status") or "Keine Aktion nötig")
            plan_why = str(first_plan_item.get("why") or "").strip()
            customer_text = str(first_plan_item.get("customer_text") or "").strip()
            payload["headline"] = status_text
            if first_plan_item.get("actor_id") == "battery_grid_charge":
                status_lines = self._lines_for_battery(gpm, first_plan_item)
                first_plan_item["status_lines"] = status_lines
                payload["status_lines"] = status_lines
                payload["summary_text"] = " ".join(status_lines)
            elif customer_text:
                payload["summary_text"] = customer_text
            elif plan_why and status_text in plan_why:
                payload["summary_text"] = plan_why
            elif plan_why:
                payload["summary_text"] = f"{status_text}. {plan_why}"
            else:
                payload["summary_text"] = status_text
        elif payload.get("mode") == "mock":
            payload["headline"] = "Demo-Ansicht"
            payload["summary_text"] = (
                "Beispielwerte zeigen den Tagesverlauf. Schalten ist in der Demo gesperrt."
            )
        else:
            payload["headline"] = "Keine Aktion nötig"
            payload["summary_text"] = (
                "Ertrag, Bedarf und Geräte werden weiter beobachtet."
            )
        return payload if True else None

    def _decision_id(
        self,
        actor_id: str,
        target: str | None,
        desired: bool | None,
        reason: str,
        input_stamp: str,
    ) -> str:
        decision_material = "|".join(
            (
                self.entry.entry_id,
                actor_id,
                target or "",
                str(desired),
                reason,
                input_stamp,
            )
        )
        return sha256(decision_material.encode()).hexdigest()[:24]

    def _actor_payload(
        self,
        actor_id: str,
        *,
        present: bool,
        desired: bool | None,
        reason: str,
        detail: str,
        source_fresh: bool,
        input_stamp: str,
        extra_ready: bool = True,
    ) -> dict[str, Any]:
        actor_spec = _ACTOR_SPECS[actor_id]
        user_smc_hold = actor_id == "battery_grid_charge" and self._user_smc_hold
        if actor_id == "battery_grid_charge":
            is_battery_switch_configured = self._battery_switch_configured()
            control_target = SMC_ENABLE_LEASE if is_battery_switch_configured else None
            current_state = self._smc_flag() if is_battery_switch_configured else None
            external_owner = False
        else:
            control_target = self.config.get(actor_spec.config_key)
            is_battery_switch_configured = isinstance(
                control_target, str
            ) and control_target.startswith("switch.")
            current_state = (
                self._actor_state(control_target)
                if is_battery_switch_configured
                else None
            )
            external_owner = False
        paused_until = self._paused_until.get(actor_id)
        is_paused = user_smc_hold or bool(paused_until and paused_until > _utcnow())
        ready_to_control = bool(
            present
            and is_battery_switch_configured
            and source_fresh
            and (desired is not None)
            and extra_ready
            and (not external_owner)
            and (not is_paused)
        )
        decision_id = self._decision_id(
            actor_id, control_target, desired, reason, input_stamp
        )
        readiness_reason = (
            "user_smc_off"
            if user_smc_hold
            else (
                "external_owner"
                if external_owner
                else "actor_paused" if is_paused else reason
            )
        )
        if user_smc_hold:
            readiness_message = _USER_SMC_OFF
        elif external_owner:
            readiness_message = "GPM steuert diesen Schalter bereits direkt."
        elif is_paused and paused_until:
            readiness_message = f"EMS pausiert bis {paused_until.astimezone(self._local_timezone()).strftime('%H:%M')} Uhr."
        else:
            readiness_message = detail
        return {
            "id": actor_id,
            "label": actor_spec.label,
            "source": actor_spec.source,
            "present": present,
            "configured": is_battery_switch_configured,
            "is_demo": False,
            "enabled": bool(self.enabled.get(actor_id)),
            "paused": is_paused,
            "paused_until": (
                paused_until.isoformat() if is_paused and paused_until else None
            ),
            "pause_reason": (
                "user_smc_off"
                if user_smc_hold
                else self._pause_reason.get(actor_id) if is_paused else None
            ),
            "current": current_state,
            "desired": desired,
            "decision": (
                "start" if desired is True else "stop" if desired is False else "hold"
            ),
            "reason": readiness_reason,
            "reason_label": _humanize_reason(readiness_reason),
            "detail": readiness_message,
            "source_fresh": source_fresh,
            "ready": ready_to_control,
            "decision_id": decision_id,
            "confirm_token": None,
            "_input_stamp": input_stamp,
        }

    def _live_payload(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        now_utc = _utcnow()
        snapshot_sections = _mapping(snapshot.get("sections"))
        overview_section = _mapping(snapshot_sections.get("overview"))
        building_section = _mapping(snapshot_sections.get("building"))
        mobility_section = _mapping(snapshot_sections.get("mobility"))
        components = self._component_presence()
        forecast_hours = self._normalize_hours(snapshot)
        energy_context = self._energy_context()
        days = self._calendar_days(forecast_hours)
        self._attach_expected_autarky(days, energy_context)
        self._attach_coverage_stack(days, components)
        context_generated_at = _parse_time(energy_context.get("generated_at"))
        data_freshness_at = _parse_time(snapshot.get("data_freshness"))
        source_freshness_stamp = (
            data_freshness_at.isoformat() if data_freshness_at else ""
        )
        gpm_data, gpm_updated_at, gpm_fresh = self._gpm()
        heat_pump_recommendation, recommendation_snapshot = self._recommendations()
        surplus_state, surplus_updated_at = self._surplus_state()
        battery_soc_percent = None
        battery_soc_entity = self.config.get(
            CONF_BATTERY_SOC_SENSOR
        ) or self.config.get(CONF_SENSOR_BATTERY_SOC)
        if isinstance(battery_soc_entity, str):
            battery_state = self.hass.states.get(battery_soc_entity)
            if (
                battery_state is not None
                and str(battery_state.attributes.get("unit_of_measurement", "")) == "%"
            ):
                battery_soc_percent = _number(
                    battery_state.state, minimum=0, maximum=100
                )
        if battery_soc_percent is None:
            battery_soc_percent = _number(
                gpm_data.get("smart_charging_current_soc"), minimum=0, maximum=100
            )
        battery_capacity_kwh = _number(
            self.config.get(CONF_BATTERY_CAPACITY), minimum=0, maximum=200
        )
        if battery_capacity_kwh is not None and battery_capacity_kwh <= 0:
            battery_capacity_kwh = None
        requested_grid_charge_kwh = _number(
            gpm_data.get("smart_charging_requested_grid_charge_kwh"),
            minimum=0,
            maximum=200,
        )
        battery_ready = bool(
            battery_soc_percent is not None
            and battery_capacity_kwh is not None
            and (requested_grid_charge_kwh is not None)
            and (requested_grid_charge_kwh <= battery_capacity_kwh)
        )
        charging_decision = str(gpm_data.get("smart_charging_decision") or "")
        grid_price_allows_charge = self._grid_price_allows_charge()
        battery_charge_desired = grid_price_allows_charge and (
            charging_decision == "load"
            or (
                gpm_data.get("smart_charging_active") is True
                and charging_decision != "wait"
            )
        )
        battery_charge_reason = str(
            gpm_data.get("smart_charging_reason")
            or gpm_data.get("smart_charging_decision")
            or "no_decision"
        )
        if not grid_price_allows_charge:
            if (
                charging_decision == "load"
                or gpm_data.get("smart_charging_active") is True
            ):
                battery_charge_reason = "price_above_threshold"
        charging_source_stamp = (
            (gpm_updated_at or data_freshness_at).isoformat()
            if gpm_updated_at or data_freshness_at
            else ""
        )
        surplus_active = bool(surplus_state is not None and surplus_state.state == "on")
        surplus_attributes = _mapping(getattr(surplus_state, "attributes", None))
        surplus_power_w = _number(
            surplus_attributes.get("surplus_power_w"), minimum=0, maximum=1000000
        )
        export_duration_s = _number(
            surplus_attributes.get("export_duration_s"), minimum=0, maximum=86400
        )
        required_export_duration_s = _number(
            surplus_attributes.get("required_duration_s"), minimum=0, maximum=86400
        )
        surplus_ready = bool(
            surplus_state is not None
            and (
                not surplus_active
                or (
                    surplus_power_w is not None
                    and export_duration_s is not None
                    and (required_export_duration_s is not None)
                    and (export_duration_s >= required_export_duration_s)
                    and (surplus_power_w >= _ACTOR_LIMITS["surplus_load"].min_surplus_w)
                )
            )
        )
        surplus_source_stamp = (
            surplus_updated_at.isoformat() if surplus_updated_at else ""
        )
        mobility_action = str(
            mobility_section.get("recommended_action") or "data_unavailable"
        )
        ev_charge_desired = mobility_action == "charge" and bool(
            mobility_section.get("charging_window_active")
        )
        mobility_data_quality = _number(
            mobility_section.get("data_quality_percent"), minimum=0, maximum=100
        )
        mobility_ready = bool(
            mobility_section.get("available")
            and mobility_data_quality is not None
            and (mobility_data_quality >= 60)
        )
        heat_pump_action = str(
            heat_pump_recommendation.get("recommended_action") or "data_unavailable"
        )
        heat_pump_desired = heat_pump_action in {"dhw", "heating", "thermal_storage"}
        heat_pump_automation_ready = bool(
            heat_pump_recommendation.get("automation_ready")
        )
        recommended_heat_action = _mapping(
            recommendation_snapshot.get("recommended_action")
        )
        heat_action_valid_until = _parse_time(
            recommended_heat_action.get("valid_until")
        )
        heat_pump_ready = bool(
            heat_pump_automation_ready
            and heat_action_valid_until is not None
            and (heat_action_valid_until > now_utc)
        )
        actors = [
            self._actor_payload(
                "battery_grid_charge",
                present=self._actor_present("battery_grid_charge", components),
                desired=battery_charge_desired,
                reason=battery_charge_reason,
                detail=f"SoC {(battery_soc_percent if battery_soc_percent is not None else '–')} %, Ziel {gpm_data.get('smart_charging_target_soc') or '–'} %",
                source_fresh=bool(
                    gpm_fresh and _fresh(gpm_updated_at, now_utc, _PRICE_MAX_AGE)
                ),
                input_stamp=charging_source_stamp,
                extra_ready=battery_ready,
            ),
            self._actor_payload(
                "surplus_load",
                present=self._actor_present("surplus_load", components),
                desired=surplus_active,
                reason=str(surplus_attributes.get("reason") or "signal_unavailable"),
                detail=f"Stabiler Überschuss {(surplus_power_w if surplus_power_w is not None else '–')} W",
                source_fresh=_fresh(surplus_updated_at, now_utc, _SURPLUS_MAX_AGE),
                input_stamp=surplus_source_stamp,
                extra_ready=surplus_ready,
            ),
            self._actor_payload(
                "wallbox",
                present=self._actor_present("wallbox", components),
                desired=ev_charge_desired,
                reason=str(
                    mobility_section.get("recommendation_reason") or mobility_action
                ),
                detail=f"PV-Anteil {mobility_section.get('expected_pv_share_percent') or '–'} %, Abfahrt {mobility_section.get('departure_time') or '–'}",
                source_fresh=_fresh(data_freshness_at, now_utc, _FORECAST_MAX_AGE),
                input_stamp=source_freshness_stamp,
                extra_ready=mobility_ready,
            ),
            self._actor_payload(
                "heat_pump_boost",
                present=self._actor_present("heat_pump_boost", components),
                desired=heat_pump_desired,
                reason=str(
                    heat_pump_recommendation.get("recommendation_reason")
                    or heat_pump_action
                ),
                detail=str(
                    heat_pump_recommendation.get("recommendation_explanation")
                    or "Kein freigegebener EAI-Wärmeintent."
                ),
                source_fresh=_fresh(data_freshness_at, now_utc, _FORECAST_MAX_AGE),
                input_stamp=f"{source_freshness_stamp}|{(heat_action_valid_until.isoformat() if heat_action_valid_until else '')}",
                extra_ready=heat_pump_ready,
            ),
        ]
        combined_summary = _mapping(days[0].get("summary")) if days else {}
        briefing = _mapping(overview_section.get("briefing"))
        payload = {
            "schema_version": 3,
            "mode": "live",
            "control_mode": self.mode,
            "control_authorized": True,
            "safety_alarm": bool(self._release_pending),
            "generated_at": now_utc.isoformat(),
            "timezone": str(self._local_timezone()),
            "freshness": source_freshness_stamp,
            "headline": str(briefing.get("headline") or "Was jetzt sinnvoll ist"),
            "summary_text": str(
                briefing.get("summary")
                or "Empfehlungen aus Prognose, Preisen und Gerätezustand."
            ),
            "summary": {
                "pv_kwh": combined_summary.get("pv_kwh"),
                "demand_kwh": combined_summary.get("demand_kwh"),
                "covered_kwh": combined_summary.get("covered_kwh"),
                "coverage_percent": combined_summary.get("coverage_percent"),
                "price_ct_kwh": _number(
                    gpm_data.get("total_price"), minimum=-1000, maximum=5000
                ),
                "battery_soc_percent": battery_soc_percent,
                "eai_data_quality_percent": _number(
                    overview_section.get("data_quality_percent"), minimum=0, maximum=100
                ),
                "eai_data_quality_origin": str(
                    overview_section.get("data_quality_origin") or "unavailable"
                ),
                "eai_sensor_coverage_percent": _number(
                    overview_section.get("sensor_coverage_percent"),
                    minimum=0,
                    maximum=100,
                ),
                "eai_learning_percent": (
                    _number(
                        building_section.get("learning_progress_percent"),
                        minimum=0,
                        maximum=100,
                    )
                    if components["heat_pump"]
                    else None
                ),
            },
            "components": components,
            "sources": [
                {
                    "id": "sfml",
                    "label": "PV-Prognose",
                    "role": "Erwarteter Solarertrag",
                    "available": bool(forecast_hours),
                    "fresh": _fresh(data_freshness_at, now_utc, _FORECAST_MAX_AGE),
                },
                {
                    "id": "stats",
                    "label": "Haus & Historie",
                    "role": "Bedarf und Überschuss",
                    "available": bool(
                        energy_context.get("available") or surplus_state is not None
                    ),
                    "fresh": bool(
                        _fresh(context_generated_at, now_utc, _FORECAST_MAX_AGE)
                        or _fresh(surplus_updated_at, now_utc, _SURPLUS_MAX_AGE)
                    ),
                },
                {
                    "id": "gpm",
                    "label": "Strompreis",
                    "role": "Preis und Ladeplan",
                    "available": bool(gpm_data),
                    "fresh": bool(
                        gpm_fresh and _fresh(gpm_updated_at, now_utc, _PRICE_MAX_AGE)
                    ),
                },
                {
                    "id": "eai",
                    "label": "Flexible Lasten",
                    "role": "Wallbox und Wärme",
                    "available": True,
                    "fresh": _fresh(data_freshness_at, now_utc, _FORECAST_MAX_AGE),
                },
            ],
            "hours": forecast_hours,
            "days": days,
            "actors": actors,
            "audit": list(self.audit)[-20:][::-1],
        }
        return self._attach_action_fields(
            payload,
            snapshot=snapshot,
            gpm=gpm_data,
            mobility=mobility_section,
            building=building_section,
        )

    def _mock_payload(self) -> dict[str, Any]:
        cache_checked_at = time.monotonic()
        cached_payload = self._mock_cache
        if (
            cached_payload is not None
            and cache_checked_at - cached_payload[0] < _MOCK_CACHE_TTL
        ):
            return copy.deepcopy(cached_payload[1])
        now_utc = _utcnow()
        components = {
            "household": True,
            "heat_pump": True,
            "wallbox": True,
            "battery": True,
        }
        local_timezone = self._local_timezone()
        now_local = now_utc.astimezone(local_timezone)
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        demo_end_utc = (local_midnight + timedelta(days=3)).astimezone(timezone.utc)
        demo_timestamp_utc = local_midnight.astimezone(timezone.utc)
        forecast_hours = []
        while demo_timestamp_utc < demo_end_utc:
            local_time = demo_timestamp_utc.astimezone(local_timezone)
            solar_shape = max(0.0, math.sin(math.pi * (local_time.hour - 6) / 12))
            pv_forecast_kwh = round(4.8 * solar_shape, 2)
            household_kwh = round(
                0.55 + (0.35 if local_time.hour in {6, 7, 18, 19, 20} else 0.1), 2
            )
            heat_pump_kwh = round(0.35 + 0.3 * solar_shape, 2)
            wallbox_kwh = round(1.2 if local_time.hour in {11, 12, 13, 14} else 0.0, 2)
            battery_reserve_kwh = round(0.5 * solar_shape, 2)
            demand_kwh = round(household_kwh + heat_pump_kwh + wallbox_kwh, 2)
            covered_kwh = round(min(pv_forecast_kwh, demand_kwh), 2)
            price_ct_kwh = round(
                27 + 11 * math.cos(math.pi * (local_time.hour - 2) / 12), 2
            )
            grid_import_kwh = round(max(demand_kwh - covered_kwh, 0.0), 3)
            grid_cost_eur = round(grid_import_kwh * price_ct_kwh / 100, 4)
            pv_after_direct_kwh = round(max(pv_forecast_kwh - covered_kwh, 0.0), 3)
            confidence_percent = round(48 + 46 * solar_shape, 1)
            forecast_hours.append(
                {
                    "timestamp": demo_timestamp_utc.isoformat(),
                    "pv_kwh": pv_forecast_kwh,
                    "demand_kwh": demand_kwh,
                    "covered_kwh": covered_kwh,
                    "household_kwh": household_kwh,
                    "heat_pump_kwh": heat_pump_kwh,
                    "wallbox_kwh": wallbox_kwh,
                    "battery_kwh": battery_reserve_kwh,
                    "battery_to_house_kwh": None,
                    "battery_to_house_actual_kwh": None,
                    "battery_to_house_forecast_kwh": None,
                    "battery_to_house_origin": "synthetic_mock",
                    "price_ct_kwh": price_ct_kwh,
                    "expected_grid_import_kwh": grid_import_kwh,
                    "expected_grid_cost_eur": grid_cost_eur,
                    "pv_potential_after_direct_kwh": pv_after_direct_kwh,
                    "coverage_percent": round(covered_kwh / demand_kwh * 100, 1),
                    "confidence_percent": confidence_percent,
                    "uncertainty_percent": round(100 - confidence_percent, 1),
                    "allocation_valid": True,
                    "household_pv_kwh": covered_kwh,
                    "heat_pump_pv_kwh": 0.0,
                    "battery_pv_reserved_kwh": 0.0,
                    "residual_pv_kwh": pv_after_direct_kwh,
                    "historical_grid_import_kwh": round(grid_import_kwh * 1.15, 3),
                }
            )
            demo_timestamp_utc += timedelta(hours=1)
        days = self._calendar_days(forecast_hours)
        self._attach_expected_autarky(days, {}, synthetic=True)
        self._attach_coverage_stack(days, components, synthetic=True)
        combined_summary = _mapping(days[0].get("summary")) if days else {}
        actors = [
            {
                "id": actor_id,
                "label": actor_spec.label,
                "source": actor_spec.source,
                "present": True,
                "configured": False,
                "is_demo": True,
                "enabled": False,
                "current": None,
                "desired": None,
                "decision": "hold",
                "reason": "license_required",
                "reason_label": _humanize_reason("license_required"),
                "paused": False,
                "paused_until": None,
                "detail": "Demo: keine Geräteschaltung möglich.",
                "source_fresh": False,
                "ready": False,
                "decision_id": "",
                "confirm_token": None,
            }
            for actor_id, actor_spec in _ACTOR_SPECS.items()
        ]
        payload = {
            "schema_version": 3,
            "mode": "mock",
            "control_mode": "observe",
            "control_authorized": False,
            "generated_at": now_utc.isoformat(),
            "timezone": str(self._local_timezone()),
            "freshness": now_utc.isoformat(),
            "headline": "Demo-Ansicht",
            "summary_text": "Beispielwerte zeigen den Tagesverlauf. Schalten ist in der Demo gesperrt.",
            "summary": {
                "pv_kwh": combined_summary.get("pv_kwh"),
                "demand_kwh": combined_summary.get("demand_kwh"),
                "covered_kwh": combined_summary.get("covered_kwh"),
                "coverage_percent": combined_summary.get("coverage_percent"),
                "price_ct_kwh": 24.7,
                "battery_soc_percent": 61,
                "eai_data_quality_percent": 92,
                "eai_data_quality_origin": "synthetic_mock",
                "eai_sensor_coverage_percent": None,
                "eai_learning_percent": 76 if components["heat_pump"] else None,
            },
            "components": components,
            "sources": [
                {
                    "id": source_key,
                    "label": source_label,
                    "role": "Demo",
                    "available": False,
                    "fresh": False,
                }
                for source_key, source_label in (
                    ("sfml", "PV-Prognose"),
                    ("stats", "Haus & Historie"),
                    ("gpm", "Strompreis"),
                    ("eai", "Flexible Lasten"),
                )
            ],
            "hours": forecast_hours,
            "days": days,
            "actors": actors,
            "audit": [],
        }
        active_demo_hours = []
        for hour_point in forecast_hours:
            local_time = datetime.fromisoformat(hour_point["timestamp"]).astimezone(
                local_timezone
            )
            if not local_time.date() == now_local.date():
                continue
            if (hour_point.get("pv_potential_after_direct_kwh") or 0) >= 0.5:
                active_demo_hours.append(hour_point)
        best_demo_pv_hour = max(
            active_demo_hours or forecast_hours[:1],
            key=lambda point: float(point.get("pv_potential_after_direct_kwh") or 0),
        )
        demo_snapshot = {
            "sections": {
                "overview": {
                    "briefing": {
                        "headline": "PV-Fenster mit Restenergie erkannt",
                        "summary": "Beispielwerte zeigen, wie das EMS Allokation und Prognosequalität erklärt.",
                    }
                },
                "forecast": {
                    "optimization": {
                        "available": bool(active_demo_hours),
                        "start": best_demo_pv_hour.get("timestamp"),
                        "duration_minutes": 120,
                        "pv_surplus_kwh": best_demo_pv_hour.get(
                            "pv_potential_after_direct_kwh"
                        ),
                        "recommendation": "Flexible Verbraucher bevorzugt in dieses PV-Fenster legen",
                        "confidence_percent": 78,
                        "explanation": {
                            "headline": "PV-Fenster mit Restenergie erkannt",
                            "summary": "Nach Hausbedarf bleibt nutzbare PV für verschiebbare Lasten.",
                            "evidence": [
                                f"PV-Prognose {combined_summary.get('pv_kwh') or 0:.2f} kWh",
                                f"Hausgrundlast {combined_summary.get('household_kwh') or 0:.2f} kWh bereits berücksichtigt",
                            ],
                        },
                        "origin": "synthetic_mock",
                    }
                },
                "energy": {
                    "household_pv_kwh": combined_summary.get("household_pv_kwh"),
                    "heat_pump_pv_kwh": combined_summary.get("heat_pump_pv_kwh"),
                    "battery_pv_reserved_kwh": combined_summary.get(
                        "battery_pv_reserved_kwh"
                    ),
                    "residual_pv_kwh": combined_summary.get("residual_pv_kwh"),
                    "unallocated_or_export_kwh": combined_summary.get(
                        "pv_potential_after_direct_kwh"
                    ),
                    "allocation_provenance": {
                        "priority_order": [
                            "household_base_load",
                            "heat_pump",
                            "battery_reserve",
                            "wallbox",
                            "grid_export",
                        ]
                    },
                },
                "building": {"indoor_c": 21.4, "target_c": 21.0, "heat_loss_kw": 1.2},
                "mobility": {
                    "available": True,
                    "departure_readiness_percent": 82,
                    "required_energy_kwh": 18.5,
                    "estimated_cost_advantage_eur": 1.4,
                    "expected_pv_share_percent": 74,
                    "departure_time": "17:30",
                    "recommendation_explanation": "Beispiel: PV-Rest vor der Abfahrt nutzen.",
                },
            }
        }
        enriched_demo_payload = self._attach_action_fields(
            payload, snapshot=demo_snapshot
        )
        self._mock_cache = (cache_checked_at, enriched_demo_payload)
        return copy.deepcopy(enriched_demo_payload)

    def _apply_demo_features(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Keep mock cards for licensed premium devices that are not configured."""
        components = _mapping(payload.get("components"))
        missing_components = {
            "heat_pump": components.get("heat_pump") is not True,
            "wallbox": components.get("wallbox") is not True,
            "battery": components.get("battery") is not True,
        }
        if not any(missing_components.values()):
            return payload
        demo_payload = self._mock_payload()
        demo_intelligence = _mapping(demo_payload.get("intelligence"))
        intelligence = payload.setdefault("intelligence", {})
        if missing_components["heat_pump"] and demo_intelligence.get("comfort"):
            intelligence["comfort"] = {
                **_mapping(demo_intelligence.get("comfort")),
                "is_demo": True,
            }
        if missing_components["wallbox"] and demo_intelligence.get("mobility"):
            intelligence["mobility"] = {
                **_mapping(demo_intelligence.get("mobility")),
                "is_demo": True,
            }
        demo_actors_by_id = {
            actor.get("id"): actor
            for actor in demo_payload.get("actors") or []
            if isinstance(actor, dict)
        }
        demo_actor_flags = {
            "heat_pump_boost": missing_components["heat_pump"],
            "wallbox": missing_components["wallbox"],
            "battery_grid_charge": missing_components["battery"],
        }
        for actor in payload.get("actors") or []:
            if demo_actor_flags.get(actor.get("id")):
                demo_actor = demo_actors_by_id.get(actor["id"], {})
                actor["present"] = True
                actor["is_demo"] = True
                actor["configured"] = False
                actor["enabled"] = False
                actor["ready"] = False
                actor["confirm_token"] = None
                if demo_actor.get("detail"):
                    actor["detail"] = demo_actor["detail"]
                if demo_actor.get("reason"):
                    actor["reason"] = demo_actor["reason"]
        return payload

    def _issue_tokens(self, payload: dict[str, Any], user_id: str) -> None:
        now_utc = _utcnow()
        self._tokens = {
            token_id: confirmation
            for token_id, confirmation in self._tokens.items()
            if confirmation.expires_at > now_utc
        }
        for actor in payload["actors"]:
            input_stamp = str(actor.pop("_input_stamp", ""))
            if not (self.mode == "confirm" and actor["ready"] and actor["enabled"]):
                continue
            existing_token_id = next(
                (
                    token_id
                    for token_id, confirmation in self._tokens.items()
                    if confirmation.user_id == user_id
                    and confirmation.actor_id == actor["id"]
                    and (confirmation.decision_id == actor["decision_id"])
                ),
                None,
            )
            token_id = existing_token_id or secrets.token_urlsafe(32)
            if existing_token_id is None:
                self._tokens[token_id] = _Token(
                    user_id=user_id,
                    entry_id=self.entry.entry_id,
                    actor_id=actor["id"],
                    desired=bool(actor["desired"]),
                    decision_id=actor["decision_id"],
                    input_stamp=input_stamp,
                    expires_at=now_utc + _TOKEN_TTL,
                )
            actor["confirm_token"] = token_id
            continue
        for plan_item in payload.get("action_plan") or []:
            if isinstance(plan_item, dict):
                actor_id = plan_item.get("actor_id")
                matching_actor = next(
                    (
                        actor
                        for actor in payload["actors"]
                        if actor.get("id") == actor_id
                    ),
                    None,
                )
                plan_item["confirm_token"] = (
                    matching_actor.get("confirm_token") if matching_actor else None
                )

    async def async_snapshot(self, user_id: str) -> dict[str, Any]:
        provider_snapshot = self._provider_snapshot()
        if provider_snapshot is None:
            if self.mode != "observe" or self._owned_targets or self._release_pending:
                async with self._lock:
                    await self._disarm("license_or_freshness_lost")
                    await self._save()
            payload = self._mock_payload()
            payload["safety_alarm"] = bool(self._release_pending)
            return payload
        payload = self._cached_live_payload(provider_snapshot)
        has_absent_enabled_actors = any(
            (
                actor.get("present") is not True
                and self.enabled.get(str(actor.get("id")), False)
                for actor in payload["actors"]
            )
        )
        owned_absent_actor_ids = {
            actor["id"]
            for actor in payload["actors"]
            if actor["id"] in self._owned_targets
            if not actor["enabled"]
            or not actor["ready"]
            or actor["desired"] is not True
        }
        if owned_absent_actor_ids or has_absent_enabled_actors:
            async with self._lock:
                await self._disable_absent_actors(payload)
                for actor_id in owned_absent_actor_ids:
                    if actor_id not in self._owned_targets:
                        continue
                    if actor_id == "battery_grid_charge" and self.enabled.get(actor_id):
                        if not self._user_smc_hold:
                            actor = next(
                                (
                                    candidate_actor
                                    for candidate_actor in payload["actors"]
                                    if candidate_actor["id"] == actor_id
                                )
                            )
                            if actor.get("ready") and actor.get("desired") is not True:
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
                        actor_id, "owned_intent_no_longer_safe", "system"
                    )
                await self._save()
            self._payload_cache = None
            payload = self._cached_live_payload(provider_snapshot)
        self._issue_tokens(payload, user_id)
        return self._apply_demo_features(payload)

    async def async_set_mode(
        self, mode: str, *, user_id: str, confirm_automatic: bool = False
    ) -> dict[str, Any]:
        if mode not in EMS_MODES:
            raise EMSControlError("invalid_mode", "Unbekannter EMS-Modus.")
        async with self._lock:
            if mode != "observe":
                if self._owned_targets or self._release_pending:
                    raise EMSControlError(
                        "safety_release_pending",
                        "Eine frühere EMS-Freigabe ist noch nicht sicher abgeschlossen.",
                    )
            if mode != "observe" and self._provider_snapshot() is None:
                raise EMSControlError(
                    "control_not_authorized",
                    "Eine frische Premium-Live-Lizenz ist erforderlich.",
                )
            if mode == "automatic":
                if not confirm_automatic:
                    raise EMSControlError(
                        "automatic_confirmation_required",
                        "Automatik muss ausdrücklich bestätigt werden.",
                    )
                payload = self._live_payload_or_error()
                if not any(
                    (
                        actor["present"] and actor["enabled"]
                        for actor in payload["actors"]
                    )
                ):
                    raise EMSControlError(
                        "actor_opt_in_required",
                        "Mindestens ein Aktor muss freigegeben sein.",
                    )
            if self.mode in {"confirm", "automatic"} and mode == "observe":
                await self._release_owned("mode_changed")
            self.mode = mode
            self._tokens.clear()
            self._audit(user_id, "mode_changed", None, None, True, mode=mode)
            await self._save()
        return await self.async_snapshot(user_id)

    async def async_set_actor(
        self, actor_id: str, enabled: bool, *, user_id: str
    ) -> dict[str, Any]:
        if actor_id not in EMS_ACTORS:
            raise EMSControlError("unknown_actor", "Unbekannter EMS-Aktor.")
        async with self._lock:
            if enabled and self._provider_snapshot() is None:
                raise EMSControlError(
                    "control_not_authorized",
                    "Aktorfreigabe erfordert Premium-Live-Daten.",
                )
            if enabled:
                payload = self._live_payload_or_error()
                actor = next(
                    (
                        candidate_actor
                        for candidate_actor in payload["actors"]
                        if candidate_actor["id"] == actor_id
                    )
                )
                if not actor["present"]:
                    raise EMSControlError(
                        "actor_not_present",
                        "Dieser Aktor ist für die vorhandene Kundenanlage nicht eingerichtet.",
                    )
            if not enabled:
                if actor_id in self._owned_targets:
                    await self._safe_off(actor_id, "actor_disabled", user_id)
            self.enabled[actor_id] = bool(enabled)
            self._tokens.clear()
            self._audit(user_id, "actor_opt_in", actor_id, enabled, True)
            await self._save()
        return await self.async_snapshot(user_id)

    async def async_confirm(self, token: str, *, user_id: str) -> dict[str, Any]:
        if not isinstance(token, str) or len(token) > 128:
            raise EMSControlError("invalid_token", "Ungültiges Bestätigungstoken.")
        async with self._lock:
            if self.mode != "confirm":
                raise EMSControlError(
                    "confirm_mode_required",
                    "EMS befindet sich nicht im Bestätigungsmodus.",
                )
            if token in self._used_tokens:
                raise EMSControlError(
                    "token_replayed", "Bestätigungstoken wurde bereits verwendet."
                )
            confirmation = self._tokens.pop(token, None)
            if confirmation is None or confirmation.expires_at <= _utcnow():
                raise EMSControlError("token_expired", "Bestätigung ist abgelaufen.")
            if (
                confirmation.user_id != user_id
                or confirmation.entry_id != self.entry.entry_id
            ):
                raise EMSControlError(
                    "token_owner_mismatch",
                    "Bestätigung gehört nicht zu diesem Benutzer.",
                )
            payload = self._live_payload_or_error()
            actor = next(
                (
                    candidate_actor
                    for candidate_actor in payload["actors"]
                    if candidate_actor["id"] == confirmation.actor_id
                )
            )
            if (
                not actor["ready"]
                or not actor["enabled"]
                or (not actor["decision_id"] == confirmation.decision_id)
                or (not actor.get("_input_stamp") == confirmation.input_stamp)
                or (not bool(actor["desired"]) == confirmation.desired)
            ):
                raise EMSControlError(
                    "decision_changed",
                    "Die Eingabedaten oder Entscheidung haben sich geändert.",
                )
            self._used_tokens.append(token)
            await self._execute_switch(
                confirmation.actor_id, confirmation.desired, "confirmed", user_id
            )
            await self._save()
        return await self.async_snapshot(user_id)

    def _live_payload_or_error(self) -> dict[str, Any]:
        snapshot = self._provider_snapshot()
        if snapshot is None:
            raise EMSControlError(
                "control_not_authorized", "Premium-Live-Daten sind nicht mehr gültig."
            )
        return self._live_payload(snapshot)

    def _payload_cache_key(self, snapshot: dict[str, Any]) -> tuple[Any, ...]:
        return (
            self.mode,
            tuple(sorted(self.enabled.items())),
            tuple(sorted(self._owned_targets.items())),
            tuple(sorted(self._release_pending)),
            self._user_smc_hold,
            self._smc_enable_set,
            tuple(
                sorted(
                    (
                        (actor_id, pause_until.isoformat())
                        for actor_id, pause_until in self._paused_until.items()
                    )
                )
            ),
            str(snapshot.get("data_freshness") or ""),
        )

    def _cached_live_payload(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        if self._release_pending:
            return self._live_payload(snapshot)
        cache_check_time = time.monotonic()
        cache_key = self._payload_cache_key(snapshot)
        cached_payload = self._payload_cache
        if (
            cached_payload is not None
            and cache_check_time - cached_payload[0] < _PAYLOAD_CACHE_TTL
            and (cached_payload[1] == cache_key)
        ):
            return copy.deepcopy(cached_payload[2])
        payload = self._live_payload(snapshot)
        self._payload_cache = (cache_check_time, cache_key, copy.deepcopy(payload))
        return payload

    async def async_auto_cycle(self) -> None:
        if self.mode != "automatic":
            return
        async with self._lock:
            snapshot = self._provider_snapshot()
            if snapshot is None:
                await self._disarm("license_or_freshness_lost")
                await self._save()
                return
            payload = self._live_payload(snapshot)
            await self._disable_absent_actors(payload)
            surplus_state, surplus_updated_at = self._surplus_state()
            surplus_active = bool(
                surplus_state is not None and surplus_state.state == "on"
            )
            surplus_power_w = (
                _number(
                    _mapping(getattr(surplus_state, "attributes", None)).get(
                        "surplus_power_w"
                    ),
                    minimum=0,
                    maximum=1000000,
                )
                or 0.0
            )
            remaining_surplus_w = surplus_power_w
            now_utc = _utcnow()
            actors = sorted(
                payload["actors"],
                key=lambda item: _ACTOR_PLAN_PRIORITY.get(str(item.get("id")), 9),
            )
            for actor in actors:
                actor_id = actor["id"]
                actor_limits = _ACTOR_LIMITS[actor_id]
                if actor_id == "battery_grid_charge":
                    self._drop_legacy_battery_lease()
                    if self._user_smc_hold:
                        continue
                paused_until = self._paused_until.get(actor_id)
                if paused_until is not None:
                    if paused_until > now_utc:
                        continue
                    self._paused_until.pop(actor_id, None)
                    self._pause_reason.pop(actor_id, None)
                if not actor["enabled"] or not actor["ready"]:
                    if actor_id in self._owned_targets:
                        await self._safe_off(
                            actor_id, "automatic_interlock_lost", "system"
                        )
                    continue
                current_state = self._actor_current(actor_id)
                owned_target = self._owned_targets.get(actor_id)
                if actor_id == "battery_grid_charge":
                    if owned_target == SMC_ENABLE_LEASE:
                        if self._smc_enable_set is not None:
                            if current_state != self._smc_enable_set:
                                if current_state is False:
                                    self._mark_user_smc_hold()
                                    self._audit(
                                        "user",
                                        "user_smc_hold",
                                        actor_id,
                                        current_state,
                                        True,
                                    )
                                else:
                                    self._owned_targets.pop(actor_id, None)
                                    self._release_pending.discard(actor_id)
                                    self._smc_enable_before = None
                                    self._smc_enable_set = None
                                    self._user_smc_hold = False
                                await self._save()
                                continue
                elif owned_target is not None and current_state is not None:
                    if current_state is not True:
                        self._pause_actor(actor_id, "manual_override")
                        self._owned_targets.pop(actor_id, None)
                        self._release_pending.discard(actor_id)
                        self._audit(
                            "system", "manual_override", actor_id, current_state, True
                        )
                        continue
                desired = bool(actor["desired"])
                if desired is False and current_state is True:
                    if owned_target is None:
                        if actor_id != "battery_grid_charge":
                            self._pause_actor(actor_id, "external_owner_paused")
                            self._tokens.clear()
                            self._audit(
                                "system", "external_owner_paused", actor_id, False, True
                            )
                            continue
                if desired and actor_limits.uses_surplus_budget:
                    if surplus_active or actor_id == "surplus_load":
                        if current_state is True:
                            remaining_surplus_w = max(
                                0.0, remaining_surplus_w - actor_limits.nominal_power_w
                            )
                        else:
                            if remaining_surplus_w < actor_limits.nominal_power_w:
                                self._audit(
                                    "system",
                                    "surplus_budget",
                                    actor_id,
                                    True,
                                    False,
                                    surplus_w=surplus_power_w,
                                )
                                continue
                            remaining_surplus_w -= actor_limits.nominal_power_w
                if desired is False and current_state is True:
                    on_since = self._on_since.get(actor_id)
                    if on_since is not None:
                        if now_utc - on_since < actor_limits.min_runtime:
                            continue
                if desired is True and current_state is False:
                    off_since = self._off_since.get(actor_id)
                    if off_since is not None:
                        if now_utc - off_since < actor_limits.min_off_time:
                            continue
                if current_state == desired:
                    continue
                try:
                    await self._execute_switch(actor_id, desired, "automatic", "system")
                except EMSControlError as control_error:
                    self._audit("system", control_error.code, actor_id, desired, False)
            await self._save()
        return None if True else None

    async def _execute_switch(
        self, actor_id: str, desired: bool, reason: str, user_id: str
    ) -> None:
        if actor_id == "battery_grid_charge":
            await self._execute_smc_enable(desired, reason, user_id)
            return
        actor_spec = _ACTOR_SPECS[actor_id]
        target_entity_id = self.config.get(actor_spec.config_key)
        if not (
            isinstance(target_entity_id, str) and target_entity_id.startswith("switch.")
        ):
            raise EMSControlError(
                "actor_not_configured", "Aktor ist nicht als Schalter konfiguriert."
            )
        now_utc = _utcnow()
        last_action_at = self._last_action.get(actor_id)
        if last_action_at is not None and now_utc - last_action_at < _COOLDOWN:
            raise EMSControlError(
                "actor_cooldown", "Aktor befindet sich in der Schaltsperre."
            )
        cycle_count = self._cycles[actor_id]
        while cycle_count and now_utc - cycle_count[0] > timedelta(hours=1):
            cycle_count.popleft()
        if len(cycle_count) >= _MAX_CYCLES_PER_HOUR:
            raise EMSControlError(
                "cycle_limit", "Maximale Schaltzyklen pro Stunde erreicht."
            )
        current_state = self._actor_state(target_entity_id)
        if current_state is None:
            raise EMSControlError(
                "actor_unavailable", "Aktorzustand ist nicht sicher lesbar."
            )
        if current_state == desired:
            if not desired:
                self._owned_targets.pop(actor_id, None)
                self._release_pending.discard(actor_id)
            return
        switch_service = "turn_on" if desired else "turn_off"
        if desired:
            self._owned_targets[actor_id] = target_entity_id
            self._release_pending.add(actor_id)
            await self._save()
        try:
            async with asyncio.timeout(_SERVICE_TIMEOUT):
                await self.hass.services.async_call(
                    "switch",
                    switch_service,
                    {"entity_id": target_entity_id},
                    blocking=True,
                )
        except TimeoutError as control_error:
            if desired and self._actor_state(target_entity_id) is False:
                self._owned_targets.pop(actor_id, None)
                self._release_pending.discard(actor_id)
                await self._save()
            self._audit(user_id, "service_timeout", actor_id, desired, False)
            raise EMSControlError(
                "service_timeout", "Home-Assistant-Schaltaufruf ist abgelaufen."
            ) from control_error
        except Exception as control_error:
            if desired and self._actor_state(target_entity_id) is False:
                self._owned_targets.pop(actor_id, None)
                self._release_pending.discard(actor_id)
                await self._save()
            self._audit(user_id, "service_failed", actor_id, desired, False)
            raise EMSControlError(
                "service_failed", "Home-Assistant-Schaltaufruf ist fehlgeschlagen."
            ) from control_error
        verification_deadline = asyncio.get_running_loop().time() + 5.0
        while (
            self._actor_state(target_entity_id) != desired
            and asyncio.get_running_loop().time() < verification_deadline
        ):
            await asyncio.sleep(0.25)
        if self._actor_state(target_entity_id) != desired:
            self._audit(user_id, "readback_failed", actor_id, desired, False)
            raise EMSControlError(
                "readback_failed", "Aktor hat den Sollzustand nicht bestätigt."
            )
        if desired:
            self._owned_targets[actor_id] = target_entity_id
            self._release_pending.discard(actor_id)
        else:
            self._owned_targets.pop(actor_id, None)
            self._release_pending.discard(actor_id)
        self._last_action[actor_id] = now_utc
        cycle_count.append(now_utc)
        if desired:
            self._on_since[actor_id] = now_utc
            self._off_since.pop(actor_id, None)
        else:
            self._off_since[actor_id] = now_utc
            self._on_since.pop(actor_id, None)
        self._payload_cache = None
        self._audit(
            user_id,
            reason,
            actor_id,
            desired,
            True,
            **self._switch_audit_detail(actor_id),
        )

    def _smc_flag(self) -> bool:
        return bool(self.config.get(CONF_SMART_CHARGING_ENABLED, False))

    def _battery_switch_configured(self) -> bool:
        target_entity_id = self.config.get(CONF_SMART_CHARGING_SWITCH)
        return isinstance(target_entity_id, str) and target_entity_id.startswith(
            "switch."
        )

    def _actor_current(self, actor_id: str) -> bool | None:
        if actor_id == "battery_grid_charge":
            if not self._battery_switch_configured():
                return
            return self._smc_flag()
        target_entity_id = self.config.get(_ACTOR_SPECS[actor_id].config_key)
        return self._actor_state(target_entity_id)

    def _drop_legacy_battery_lease(self) -> None:
        """Forget an old charge-switch lease without calling the switch."""
        legacy_target = self._owned_targets.get("battery_grid_charge")
        if isinstance(legacy_target, str) and legacy_target.startswith("switch."):
            self._owned_targets.pop("battery_grid_charge", None)
            self._release_pending.discard("battery_grid_charge")
            self._audit(
                "system",
                "legacy_charge_switch_lease_dropped",
                "battery_grid_charge",
                None,
                True,
            )
        return None if True else None

    def _mark_user_smc_hold(self) -> None:
        """User owns the enable flag until they turn Smart Charging back on."""
        self._user_smc_hold = True
        self._owned_targets.pop("battery_grid_charge", None)
        self._release_pending.discard("battery_grid_charge")
        self._smc_enable_before = None
        self._smc_enable_set = None
        self._paused_until.pop("battery_grid_charge", None)
        self._pause_reason["battery_grid_charge"] = "user_smc_off"
        self._tokens.clear()
        self._payload_cache = None

    async def note_external_smc_enable(self, enabled: bool) -> None:
        """Record an enable-flag change that did not come from EMS."""
        async with self._lock:
            self.config[CONF_SMART_CHARGING_ENABLED] = bool(enabled)
            self._payload_cache = None
            if enabled:
                if self._user_smc_hold:
                    self._user_smc_hold = False
                    self._pause_reason.pop("battery_grid_charge", None)
                    self._audit(
                        "user", "user_smc_resumed", "battery_grid_charge", True, True
                    )
                if self._owned_targets.get("battery_grid_charge") == SMC_ENABLE_LEASE:
                    self._owned_targets.pop("battery_grid_charge", None)
                    self._release_pending.discard("battery_grid_charge")
                    self._smc_enable_before = None
                    self._smc_enable_set = None
                await self._save()
                return
            ems_owns_smc_enable = (
                self._owned_targets.get("battery_grid_charge") == SMC_ENABLE_LEASE
                or (
                    self.mode == "automatic"
                    and bool(self.enabled.get("battery_grid_charge"))
                )
                or self._user_smc_hold
            )
            if ems_owns_smc_enable:
                self._mark_user_smc_hold()
                self._audit("user", "user_smc_hold", "battery_grid_charge", False, True)
            await self._save()
        return None if True else None

    async def _execute_smc_enable(
        self, desired: bool, reason: str, user_id: str
    ) -> None:
        """Set the SMC enable flag. Never calls the charge switch."""
        actor_id = "battery_grid_charge"
        self._drop_legacy_battery_lease()
        if self._user_smc_hold:
            raise EMSControlError("user_smc_off", _USER_SMC_OFF)
        if not self._battery_switch_configured():
            raise EMSControlError(
                "actor_not_configured", "Smart Charging ist nicht konfiguriert."
            )
        now_utc = _utcnow()
        last_action_at = self._last_action.get(actor_id)
        if last_action_at is not None and now_utc - last_action_at < _COOLDOWN:
            raise EMSControlError(
                "actor_cooldown", "Aktor befindet sich in der Schaltsperre."
            )
        cycle_count = self._cycles[actor_id]
        while cycle_count and now_utc - cycle_count[0] > timedelta(hours=1):
            cycle_count.popleft()
        if len(cycle_count) >= _MAX_CYCLES_PER_HOUR:
            raise EMSControlError(
                "cycle_limit", "Maximale Schaltzyklen pro Stunde erreicht."
            )
        current_state = self._smc_flag()
        if current_state == desired:
            return
        if actor_id not in self._owned_targets:
            self._smc_enable_before = current_state
        self._owned_targets[actor_id] = SMC_ENABLE_LEASE
        self._release_pending.add(actor_id)
        await self._save()
        try:
            async with asyncio.timeout(_SERVICE_TIMEOUT):
                service_result = await async_set_enabled(self.hass, desired)
        except TimeoutError as control_error:
            if desired and (not self._smc_flag()):
                self._owned_targets.pop(actor_id, None)
                self._release_pending.discard(actor_id)
                self._smc_enable_before = None
            else:
                self._release_pending.add(actor_id)
            await self._save()
            self._audit(user_id, "service_timeout", actor_id, desired, False)
            raise EMSControlError(
                "service_timeout", "Home-Assistant-Schaltaufruf ist abgelaufen."
            ) from control_error
        except Exception as control_error:
            if desired and (not self._smc_flag()):
                self._owned_targets.pop(actor_id, None)
                self._release_pending.discard(actor_id)
                self._smc_enable_before = None
            else:
                self._release_pending.add(actor_id)
            await self._save()
            self._audit(user_id, "service_failed", actor_id, desired, False)
            raise EMSControlError(
                "service_failed", "Home-Assistant-Schaltaufruf ist fehlgeschlagen."
            ) from control_error
        if isinstance(service_result, dict) and service_result.get("errors"):
            if desired and (not self._smc_flag()):
                self._owned_targets.pop(actor_id, None)
                self._release_pending.discard(actor_id)
                self._smc_enable_before = None
            else:
                self._release_pending.add(actor_id)
            await self._save()
            self._audit(user_id, "service_failed", actor_id, desired, False)
            raise EMSControlError(
                "service_failed", "Smart Charging hat die Freigabe nicht übernommen."
            )
        reported_enabled_flag = (
            service_result.get(CONF_SMART_CHARGING_ENABLED)
            if isinstance(service_result, dict)
            else None
        )
        if reported_enabled_flag is None:
            self.config[CONF_SMART_CHARGING_ENABLED] = bool(desired)
        else:
            self.config[CONF_SMART_CHARGING_ENABLED] = bool(reported_enabled_flag)
        if self._smc_flag() != bool(desired):
            self._release_pending.add(actor_id)
            self._audit(user_id, "readback_failed", actor_id, desired, False)
            raise EMSControlError(
                "readback_failed", "Aktor hat den Sollzustand nicht bestätigt."
            )
        self._smc_enable_set = bool(desired)
        self._owned_targets[actor_id] = SMC_ENABLE_LEASE
        self._release_pending.discard(actor_id)
        self._last_action[actor_id] = now_utc
        cycle_count.append(now_utc)
        if desired:
            self._on_since[actor_id] = now_utc
            self._off_since.pop(actor_id, None)
        else:
            self._off_since[actor_id] = now_utc
            self._on_since.pop(actor_id, None)
        self._payload_cache = None
        self._audit(
            user_id,
            reason,
            actor_id,
            desired,
            True,
            **self._switch_audit_detail(actor_id),
        )

    async def _restore_smc_enable(self, reason: str, user_id: str) -> bool:
        """Return the enable flag from before EMS took it, if EMS still owns it."""
        actor_id = "battery_grid_charge"
        self._drop_legacy_battery_lease()
        if self._user_smc_hold:
            self._owned_targets.pop(actor_id, None)
            self._release_pending.discard(actor_id)
            return True
        if self._owned_targets.get(actor_id) != SMC_ENABLE_LEASE:
            self._release_pending.discard(actor_id)
            return True
        current_state = self._smc_flag()
        if self._smc_enable_set is not None and current_state != self._smc_enable_set:
            self._owned_targets.pop(actor_id, None)
            self._release_pending.discard(actor_id)
            self._smc_enable_before = None
            self._smc_enable_set = None
            self._audit(user_id, f"{reason}_user_kept", actor_id, current_state, True)
            return True
        previous_enabled_flag = self._smc_enable_before
        if previous_enabled_flag is None or current_state == previous_enabled_flag:
            self._owned_targets.pop(actor_id, None)
            self._release_pending.discard(actor_id)
            self._smc_enable_before = None
            self._smc_enable_set = None
            self._audit(user_id, reason, actor_id, current_state, True)
            return True
        try:
            async with asyncio.timeout(_SERVICE_TIMEOUT):
                service_result = await async_set_enabled(
                    self.hass, previous_enabled_flag
                )
        except Exception:
            self._release_pending.add(actor_id)
            self._audit(
                user_id,
                f"{reason}_release_failed",
                actor_id,
                previous_enabled_flag,
                False,
            )
            return False
        if isinstance(service_result, dict) and service_result.get("errors"):
            self._release_pending.add(actor_id)
            self._audit(
                user_id,
                f"{reason}_release_failed",
                actor_id,
                previous_enabled_flag,
                False,
            )
            return False
        reported_enabled_flag = (
            service_result.get(CONF_SMART_CHARGING_ENABLED)
            if isinstance(service_result, dict)
            else None
        )
        if reported_enabled_flag is None:
            self.config[CONF_SMART_CHARGING_ENABLED] = bool(previous_enabled_flag)
        else:
            self.config[CONF_SMART_CHARGING_ENABLED] = bool(reported_enabled_flag)
        if self._smc_flag() != bool(previous_enabled_flag):
            self._release_pending.add(actor_id)
            self._audit(
                user_id,
                f"{reason}_release_readback_failed",
                actor_id,
                previous_enabled_flag,
                False,
            )
            return False
        self._owned_targets.pop(actor_id, None)
        self._release_pending.discard(actor_id)
        self._smc_enable_before = None
        self._smc_enable_set = None
        self._last_action[actor_id] = _utcnow()
        self._off_since[actor_id] = _utcnow()
        self._on_since.pop(actor_id, None)
        self._payload_cache = None
        self._audit(user_id, reason, actor_id, bool(previous_enabled_flag), True)
        return True

    async def _release_owned(self, reason: str) -> None:
        for actor_id in tuple(self._owned_targets):
            await self._safe_off(actor_id, reason, "system")

    async def _disarm(self, reason: str) -> None:
        await self._release_owned(reason)
        self.mode = "observe"
        self._tokens.clear()
        self._payload_cache = None
        release_complete = not self._release_pending
        self._audit("system", "automatic_disarmed", None, None, release_complete)
        if reason in _NOTIFY_DISARM_REASONS:
            await self._notify(
                "sfml_stats_ems_disarmed",
                "EMS auf Beobachten gestellt",
                "Das Energiemanagement hat die Steuerung sicher beendet, weil Freigabe oder Datenfrische fehlen.",
            )
        if self._release_pending:
            await self._notify(
                "sfml_stats_ems_safety_alarm",
                "EMS-Sicherheitsfreigabe ausstehend",
                "Ein vom EMS übernommenes Gerät konnte noch nicht bestätigt ausgeschaltet werden.",
            )
        return None if True else None

    async def _safe_off(self, actor_id: str, reason: str, user_id: str) -> bool:
        """Release one concrete EMS-owned target without normal cycle limits."""
        if actor_id == "battery_grid_charge":
            return await self._restore_smc_enable(reason, user_id)
        owned_target = self._owned_targets.get(actor_id)
        if not owned_target:
            self._release_pending.discard(actor_id)
            return True
        current_state = self._actor_state(owned_target)
        if current_state is False:
            self._owned_targets.pop(actor_id, None)
            self._release_pending.discard(actor_id)
            self._audit(user_id, reason, actor_id, False, True)
            return True
        if current_state is None:
            self._release_pending.add(actor_id)
            self._audit(user_id, f"{reason}_release_unreadable", actor_id, False, False)
            return False
        try:
            async with asyncio.timeout(_SERVICE_TIMEOUT):
                await self.hass.services.async_call(
                    "switch", "turn_off", {"entity_id": owned_target}, blocking=True
                )
        except Exception:
            self._release_pending.add(actor_id)
            self._audit(user_id, f"{reason}_release_failed", actor_id, False, False)
            return False
        verification_deadline = asyncio.get_running_loop().time() + 5.0
        while (
            self._actor_state(owned_target) is not False
            and asyncio.get_running_loop().time() < verification_deadline
        ):
            await asyncio.sleep(0.25)
        if self._actor_state(owned_target) is not False:
            self._release_pending.add(actor_id)
            self._audit(
                user_id, f"{reason}_release_readback_failed", actor_id, False, False
            )
            return False
        self._owned_targets.pop(actor_id, None)
        self._release_pending.discard(actor_id)
        self._last_action[actor_id] = _utcnow()
        self._off_since[actor_id] = _utcnow()
        self._on_since.pop(actor_id, None)
        self._payload_cache = None
        self._audit(user_id, reason, actor_id, False, True)
        return True

    def _audit(
        self,
        actor: str,
        event: str,
        actor_id: str | None,
        desired: bool | None,
        success: bool,
        **detail: Any,
    ) -> None:
        self.audit.append(
            {
                "timestamp": _utcnow().isoformat(),
                "event": event[:80],
                "actor": self._audit_actor(actor),
                "actor_id": actor_id,
                "desired": desired,
                "success": success,
                **{
                    detail_key: detail_value
                    for detail_key, detail_value in detail.items()
                    if detail_key in _AUDIT_DETAIL_KEYS
                },
            }
        )
        if "label" not in self.audit[-1]:
            self.audit[-1]["label"] = _humanize_reason(event)
        return None if True else None

    def _audit_actor(self, actor: str) -> str:
        """Store a stable local pseudonym instead of a Home Assistant user id."""
        if actor == "system":
            return "system"
        audit_actor_hash = sha256(
            f"{self.entry.entry_id}|{actor}".encode()
        ).hexdigest()[:12]
        return f"user-{audit_actor_hash}"

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "mode": self.mode,
                "enabled": dict(self.enabled),
                "audit": list(self.audit),
                "owned_targets": dict(self._owned_targets),
                "smc_enable_before": self._smc_enable_before,
                "smc_enable_set": self._smc_enable_set,
                "user_smc_hold": self._user_smc_hold,
                "release_pending": sorted(self._release_pending),
                "paused_until": {
                    actor_id: pause_until.isoformat()
                    for actor_id, pause_until in self._paused_until.items()
                },
                "pause_reason": dict(self._pause_reason),
            }
        )

    def _pause_actor(self, actor_id: str, reason: str) -> None:
        self._paused_until[actor_id] = _utcnow() + _MANUAL_PAUSE
        self._pause_reason[actor_id] = reason
        self._tokens.clear()
        self._payload_cache = None

    def _switch_audit_detail(self, actor_id: str) -> dict[str, Any]:
        surplus_state, surplus_updated_at = self._surplus_state()
        surplus_power_w = _number(
            _mapping(getattr(surplus_state, "attributes", None)).get("surplus_power_w"),
            minimum=0,
            maximum=1000000,
        )
        gpm_data, gpm_updated_at, gpm_fresh = self._gpm()
        battery_soc_entity = self.config.get(
            CONF_BATTERY_SOC_SENSOR
        ) or self.config.get(CONF_SENSOR_BATTERY_SOC)
        battery_soc_percent = None
        if isinstance(battery_soc_entity, str):
            entity_state = self.hass.states.get(battery_soc_entity)
            if entity_state is not None:
                battery_soc_percent = _number(
                    entity_state.state, minimum=0, maximum=100
                )
        return {
            "surplus_w": surplus_power_w,
            "price_ct_kwh": _number(
                gpm_data.get("total_price"), minimum=-1000, maximum=5000
            ),
            "soc_percent": battery_soc_percent,
        }

    async def _notify(self, notification_id: str, title: str, message: str) -> None:
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "notification_id": notification_id[:64],
                    "title": title[:80],
                    "message": message[:240],
                },
                blocking=False,
            )
        except Exception:
            return


def get_ems_manager(hass: Any) -> EMSManager | None:
    for source_entry in hass.data.get(DOMAIN, {}).values():
        if not isinstance(source_entry, dict):
            continue
        if not isinstance(source_entry.get("ems_manager"), EMSManager):
            continue
        return source_entry["ems_manager"]
