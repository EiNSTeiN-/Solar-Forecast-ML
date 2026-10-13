# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Passive, explainable automation signals for EAI."""
from __future__ import annotations
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from .const import (
    CONF_DHW_TEMP_ENTITY,
    CONF_STORAGE_AMBIENT_TEMP_ENTITY,
    CONF_STORAGE_VOLUME_L,
    CONF_ELECTRICITY_PRICE_ENTITY,
    CONF_ELECTRICITY_PRICE_UNIT,
    CONF_FEED_IN_TARIFF_ENTITY,
    CONF_FEED_IN_TARIFF_UNIT,
    CONF_HAS_DHW,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_LOW_PRICE_THRESHOLD_CT,
    CONF_STORAGE_TEMP_ENTITY,
    CONF_TARGET_TEMP_ENTITY,
    CONF_WALLBOX_ENABLED,
    DEFAULT_LOW_PRICE_THRESHOLD_CT,
    DOMAIN,
    INTEGRATION_MODEL,
)
from .setup_state import apply_recommendation_choke, current_dhw_temperatures, is_true

_LOGGER = logging.getLogger(__name__)
POLICY_DEFAULTS = {
    "automation_signals": False,
    "pv_optimization": True,
    "tariff_optimization": True,
    "wallbox_optimization": True,
}


def wallbox_enabled(entry: Any) -> bool:
    return bool(
        entry.options.get(CONF_WALLBOX_ENABLED, entry.data.get(CONF_WALLBOX_ENABLED))
    )


def thermal_loss_enabled(entry: Any) -> bool:
    merged_config = {**entry.data, **entry.options}
    return bool(
        merged_config.get(CONF_STORAGE_VOLUME_L)
        and merged_config.get(CONF_STORAGE_AMBIENT_TEMP_ENTITY)
        and (
            merged_config.get(CONF_STORAGE_TEMP_ENTITY)
            or merged_config.get(CONF_DHW_TEMP_ENTITY)
        )
    )


def is_legacy_eai_unique_id(entry_id: str, unique_id: str) -> bool:
    return unique_id.startswith(f"{entry_id}_ml_") or unique_id in {
        f"{entry_id}_drift_status",
        f"{entry_id}_active_prediction_model",
        f"{entry_id}_physics_samples",
    }


@dataclass(frozen=True, slots=True)
class RecommendationSnapshot:
    values: dict[str, Any]
    attributes: dict[str, dict[str, Any]]


def device_info(entry_id: str) -> dict[str, Any]:
    return {
        "identifiers": {(DOMAIN, entry_id)},
        "name": "Solar Forecast Energy AI",
        "manufacturer": "Solar Forecast ML",
        "model": INTEGRATION_MODEL,
    }


class EAIRecommendationEngine:
    """Translate EAI analysis into passive Home Assistant entity states."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.hass = runtime.hass
        self.entry = runtime.entry
        self._listeners = set()
        self._coordinator_unsub = None
        self._state_unsub = None
        self._state_entity_ids = ()
        self._cache = None
        self._connected = True
        self.policies = dict(POLICY_DEFAULTS)

    def connect(self, coordinator: Any | None) -> None:
        if coordinator is None:
            return
        self._connected = True
        add_coordinator_listener = getattr(coordinator, "async_add_listener", None)
        if callable(add_coordinator_listener) and self._coordinator_unsub is None:
            self._coordinator_unsub = add_coordinator_listener(self.invalidate)
        self.reconcile_sources()
        self.refresh()

    def _configured_entity_ids(self) -> tuple[str, ...]:
        merged_config = {
            **getattr(self.entry, "data", {}),
            **getattr(self.entry, "options", {}),
        }
        return tuple(
            sorted(
                {
                    entity_value
                    for config_key, entity_value in merged_config.items()
                    if config_key.endswith("_entity")
                    if isinstance(entity_value, str)
                    if entity_value
                }
            )
        )

    def reconcile_sources(self) -> bool:
        """Repair the state subscription when configured sources change."""
        if not self._connected:
            return False
        source_entity_ids = self._configured_entity_ids()
        if (
            self._state_unsub is not None
            and source_entity_ids == self._state_entity_ids
        ):
            return False
        if self._state_unsub is not None:
            self._state_unsub()
            self._state_unsub = None
        self._state_entity_ids = source_entity_ids
        if source_entity_ids:
            from homeassistant.helpers.event import (
                async_track_state_change_event as async_track_state_change_event,
            )

            self._state_unsub = async_track_state_change_event(
                self.hass, source_entity_ids, lambda _event: self.invalidate()
            )
        return True

    def disconnect(self) -> None:
        self._connected = False
        if self._coordinator_unsub is not None:
            self._coordinator_unsub()
            self._coordinator_unsub = None
        if self._state_unsub is not None:
            self._state_unsub()
            self._state_unsub = None
        self._state_entity_ids = ()
        self._listeners.clear()
        self._cache = None

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    def set_policy(self, key: str, enabled: bool) -> None:
        if key not in self.policies or self.policies[key] == enabled:
            return None
        self.policies[key] = enabled
        self.invalidate()

    def invalidate(self, *, force_notify: bool = False) -> None:
        self.hass.loop.call_soon_threadsafe(
            self._invalidate_on_event_loop, force_notify
        )

    def _invalidate_on_event_loop(self, force_notify: bool = False) -> None:
        if not self._connected:
            return
        if self._cache is None and (not force_notify) and (not self._listeners):
            return
        self.refresh(force_notify=True)

    def refresh(self, *, force_notify: bool = False) -> bool:
        """Rebuild the shared snapshot before Home Assistant reads entities."""
        if not self._connected:
            return False
        previous_snapshot = self._cache
        try:
            self._cache = self._build_snapshot()
        except Exception:
            self._cache = previous_snapshot
            _LOGGER.exception("EAI recommendation snapshot refresh failed")
            return False
        if not force_notify:
            return True
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception("EAI recommendation listener refresh failed")
                continue
        return True

    def snapshot(self) -> RecommendationSnapshot:
        if self._cache is None:
            self._cache = self._build_snapshot()
        return self._cache

    def _build_snapshot(self) -> RecommendationSnapshot:
        provider_snapshot = self.runtime.provider.snapshot()
        sections = provider_snapshot.get("sections") or {}
        overview = sections.get("overview") or {}
        forecast = sections.get("forecast") or {}
        diagnostics = sections.get("diagnostics") or {}
        operation = sections.get("operation") or {}
        building = sections.get("building") or {}
        mobility = sections.get("mobility") or {}
        thermal_loss = building.get("thermal_loss") or {}
        forecast_hours = (
            forecast.get("hours") if isinstance(forecast.get("hours"), list) else []
        )
        merged_config = {**self.entry.data, **self.entry.options}
        now = datetime.now(timezone.utc)
        diagnostic_checks = diagnostics.get("checks") or {}
        diagnostics_unlocked = not diagnostics.get("locked", False)
        data_ready = diagnostics.get("data_ready")
        required_data_missing = bool(
            diagnostics.get("available") is False
            or data_ready is False
            or (
                data_ready is None
                and diagnostic_checks.get("required_sensors") is False
            )
        )
        critical_data_issue = bool(
            provider_snapshot.get("runtime", {}).get("status") != "active"
            or (diagnostics_unlocked and required_data_missing)
        )
        data_quality_percent = int(overview.get("data_quality_percent") or 0)
        forecast_usable = bool(
            provider_snapshot.get("license_status") == "valid"
            and provider_snapshot.get("configured")
            and forecast.get("available")
            and forecast_hours
            and (data_quality_percent >= 60)
            and (not critical_data_issue)
        )
        master_policy_enabled = self.policies["automation_signals"]
        automation_ready = forecast_usable and master_policy_enabled
        current_window, best_window = self._forecast_windows(forecast_hours, now)
        current_surplus_kwh = self._surplus(current_window)
        best_surplus_kwh = self._surplus(best_window)
        current_pv_window = bool(current_window and current_surplus_kwh >= 0.5)
        electricity_price_ct = self._price_ct(
            merged_config.get(CONF_ELECTRICITY_PRICE_ENTITY),
            merged_config.get(CONF_ELECTRICITY_PRICE_UNIT),
        )
        feed_in_tariff_ct = self._price_ct(
            merged_config.get(CONF_FEED_IN_TARIFF_ENTITY),
            merged_config.get(CONF_FEED_IN_TARIFF_UNIT),
        )
        low_price_threshold_ct = float(
            merged_config.get(
                CONF_LOW_PRICE_THRESHOLD_CT, DEFAULT_LOW_PRICE_THRESHOLD_CT
            )
        )
        low_price_window = (
            electricity_price_ct is not None
            and electricity_price_ct <= low_price_threshold_ct
        )
        if self.policies["pv_optimization"] and current_pv_window:
            candidate_window = current_window
        elif self.policies["tariff_optimization"] and low_price_window:
            candidate_window = current_window or best_window
        elif self.policies["pv_optimization"] and best_surplus_kwh >= 0.5:
            candidate_window = best_window
        else:
            candidate_window = current_window or best_window
        candidate_surplus_kwh = self._surplus(candidate_window)
        candidate_pv_window = bool(candidate_window and candidate_surplus_kwh >= 0.5)
        indoor_temp_c = self._state_number(
            merged_config.get(CONF_INDOOR_TEMP_ENTITY),
            climate_attribute="current_temperature",
        )
        target_temp_c = self._state_number(
            merged_config.get(CONF_TARGET_TEMP_ENTITY), climate_attribute="temperature"
        )
        comfort_need = (
            indoor_temp_c is not None
            and target_temp_c is not None
            and (indoor_temp_c < target_temp_c - 0.2)
        )
        has_dhw = is_true(merged_config, CONF_HAS_DHW)
        has_storage_sensor = bool(merged_config.get(CONF_STORAGE_TEMP_ENTITY))
        optimization_window = bool(
            self.policies["pv_optimization"]
            and candidate_pv_window
            or (self.policies["tariff_optimization"] and low_price_window)
        )
        recommended_action = "none"
        recommendation_reason = "none"
        if not forecast_usable:
            recommendation_reason = "data_unavailable"
        elif comfort_need:
            recommended_action, recommendation_reason = ("heating", "comfort_need")
        elif (
            optimization_window and has_dhw and merged_config.get(CONF_DHW_TEMP_ENTITY)
        ):
            recommended_action = "dhw"
            recommendation_reason = "pv_surplus" if candidate_pv_window else "low_price"
        elif optimization_window and has_storage_sensor:
            recommended_action = "thermal_storage"
            recommendation_reason = "pv_surplus" if candidate_pv_window else "low_price"
        elif (
            electricity_price_ct is not None
            and electricity_price_ct > low_price_threshold_ct
            and (operation.get("compressor_on") is False)
        ):
            recommended_action, recommendation_reason = ("defer", "high_price")
        dhw_temp_c, storage_temp_c = current_dhw_temperatures(self.hass, merged_config)
        recommended_action, recommendation_reason = apply_recommendation_choke(
            merged_config,
            recommended_action,
            recommendation_reason,
            dhw_temp_c=dhw_temp_c,
            storage_temp_c=storage_temp_c,
        )
        action_start = (
            self._point_time(candidate_window)
            if candidate_window and recommended_action != "none"
            else None
        )
        if recommended_action == "heating":
            action_start = now
        valid_until = (action_start or now) + timedelta(minutes=60)
        duration_minutes = 60 if recommended_action != "none" else None
        window_confidence_percent = self._number(
            (candidate_window or {}).get("confidence_percent")
        )
        forecast_uncertainty_percent = self._number(
            (candidate_window or {}).get("uncertainty_percent")
        )
        if forecast_uncertainty_percent is None:
            forecast_uncertainty_percent = self._number(
                (forecast.get("uncertainty") or {}).get("average_percent")
            )
        recommendation_confidence_percent = min(
            data_quality_percent,
            (
                int(window_confidence_percent)
                if window_confidence_percent is not None
                else max(
                    0,
                    data_quality_percent
                    - (15 if forecast.get("quality") != "ready" else 0),
                )
            ),
        )
        estimated_cost_advantage_eur = None
        if electricity_price_ct is not None and candidate_surplus_kwh > 0:
            estimated_cost_advantage_eur = round(
                candidate_surplus_kwh
                * max(electricity_price_ct - (feed_in_tariff_ct or 0), 0)
                / 100,
                2,
            )
        explanation = self._recommendation_explanation(
            action=recommended_action,
            reason=recommendation_reason,
            surplus=candidate_surplus_kwh,
            confidence=recommendation_confidence_percent,
            uncertainty=forecast_uncertainty_percent,
            price=electricity_price_ct,
            threshold=low_price_threshold_ct,
            candidate=candidate_window,
        )
        mobility_explanation = mobility.get("recommendation_explanation") or {}
        signals_enabled = automation_ready
        wallbox_signals_enabled = bool(
            signals_enabled
            and self.policies["wallbox_optimization"]
            and mobility.get("available")
        )
        snapshot_values = {
            "recommended_action": recommended_action,
            "recommendation_reason": recommendation_reason,
            "recommendation_explanation": explanation["summary"],
            "recommendation_confidence": recommendation_confidence_percent,
            "forecast_uncertainty": forecast_uncertainty_percent,
            "next_action_start": action_start,
            "recommendation_valid_until": valid_until,
            "recommended_duration": duration_minutes,
            "consumption_next_hour": self._number(
                (current_window or candidate_window or {}).get("forecast_kw")
            ),
            "consumption_today": self._number(overview.get("expected_today_kwh")),
            "consumption_tomorrow": self._number(overview.get("expected_tomorrow_kwh")),
            "expected_pv_surplus": round(max(0.0, candidate_surplus_kwh), 2),
            "estimated_cost_advantage": estimated_cost_advantage_eur,
            "data_quality": data_quality_percent,
            "model_status": diagnostics.get("model_status")
            or ("ready" if forecast.get("quality") == "ready" else "learning"),
            "automation_ready": automation_ready,
            "pv_window_active": signals_enabled
            and self.policies["pv_optimization"]
            and current_pv_window,
            "dhw_recommended": signals_enabled and recommended_action == "dhw",
            "heating_recommended": signals_enabled and recommended_action == "heating",
            "thermal_storage_recommended": signals_enabled
            and recommended_action == "thermal_storage",
            "low_price_window_active": signals_enabled
            and self.policies["tariff_optimization"]
            and low_price_window,
            "operation_deferrable": signals_enabled and recommended_action == "defer",
            "critical_data_issue": critical_data_issue,
            "wallbox_recommended_action": mobility.get(
                "recommended_action", "data_unavailable"
            ),
            "wallbox_recommendation_reason": mobility.get(
                "recommendation_reason", "data_unavailable"
            ),
            "wallbox_recommendation_explanation": mobility_explanation.get(
                "summary", "Noch keine belastbare Wallbox-Empfehlung verfügbar."
            ),
            "wallbox_recommendation_confidence": self._number(
                mobility.get("recommendation_confidence_percent")
            ),
            "wallbox_forecast_uncertainty": self._number(
                mobility.get("forecast_uncertainty_percent")
            ),
            "wallbox_next_start": self._datetime(mobility.get("recommended_start")),
            "wallbox_recommended_end": self._datetime(mobility.get("recommended_end")),
            "wallbox_required_energy": self._number(
                mobility.get("required_energy_kwh")
            ),
            "wallbox_expected_pv_share": self._number(
                mobility.get("expected_pv_share_percent")
            ),
            "wallbox_estimated_cost": self._number(
                mobility.get("estimated_advised_cost_eur")
            ),
            "wallbox_estimated_cost_advantage": self._number(
                mobility.get("estimated_cost_advantage_eur")
            ),
            "wallbox_departure_readiness": self._number(
                mobility.get("departure_readiness_percent")
            ),
            "wallbox_data_quality": self._number(mobility.get("data_quality_percent")),
            "storage_heat_loss_coefficient": self._number(
                thermal_loss.get("standby_loss_coefficient_w_k")
            ),
            "storage_standby_loss": self._number(
                thermal_loss.get("standby_loss_kwh_day")
            ),
            "circulation_loss": self._number(
                thermal_loss.get("circulation_loss_kwh_day")
            ),
            "thermal_loss_forecast": self._number(
                thermal_loss.get("forecast_thermal_loss_kwh_24h")
            ),
            "thermal_loss_data_quality": self._number(
                thermal_loss.get("data_quality_percent")
            ),
            "thermal_loss_status": thermal_loss.get("status", "learning"),
            "wallbox_charging_window_active": wallbox_signals_enabled
            and bool(mobility.get("charging_window_active")),
            "wallbox_pv_charging_recommended": wallbox_signals_enabled
            and bool(mobility.get("pv_charging_recommended")),
            "wallbox_residual_pv_window_active": wallbox_signals_enabled
            and bool(mobility.get("residual_pv_window_active")),
            "wallbox_low_price_charging_recommended": wallbox_signals_enabled
            and bool(mobility.get("low_price_charging_recommended")),
            "wallbox_departure_risk": wallbox_signals_enabled
            and bool(mobility.get("departure_risk")),
        }
        intent_signature = f"{recommended_action}|{recommendation_reason}|{(action_start.isoformat() if action_start else '')}|{valid_until.isoformat()}|{mobility.get('recommended_action')}|{mobility.get('recommended_start')}"
        intent_attributes = {
            "intent_id": sha256(intent_signature.encode()).hexdigest()[:16],
            "passive_signal": True,
            "control_services_called": False,
            "master_enabled": master_policy_enabled,
        }
        snapshot_attributes = {
            entity_key: intent_attributes
            | {"reason": recommendation_reason, "valid_until": valid_until.isoformat()}
            for entity_key in snapshot_values
        }
        snapshot_attributes["estimated_cost_advantage"] |= {
            "electricity_price_ct_per_kwh": electricity_price_ct,
            "feed_in_tariff_ct_per_kwh": feed_in_tariff_ct,
        }
        for entity_key in (
            "recommended_action",
            "recommendation_reason",
            "recommendation_explanation",
            "recommendation_confidence",
            "forecast_uncertainty",
        ):
            snapshot_attributes[entity_key] |= explanation
        for entity_key in snapshot_values:
            if entity_key.startswith("wallbox_"):
                snapshot_attributes[entity_key] |= {
                    "advisory_only": True,
                    "reason": mobility.get("recommendation_reason", "data_unavailable"),
                    "valid_until": mobility.get("recommended_end"),
                    "allocation_origin": mobility.get("allocation_origin"),
                    "departure_time": mobility.get("departure_time"),
                    "demand_source": mobility.get("demand_source"),
                    "demand_confidence_percent": mobility.get(
                        "demand_confidence_percent"
                    ),
                    "battery_energy_demand_kwh": mobility.get(
                        "battery_energy_demand_kwh"
                    ),
                    "charging_efficiency_percent": mobility.get(
                        "charging_efficiency_percent"
                    ),
                    "explanation_headline": mobility_explanation.get("headline"),
                    "explanation_evidence": mobility_explanation.get("evidence", []),
                    "recommendation_confidence_percent": mobility.get(
                        "recommendation_confidence_percent"
                    ),
                    "forecast_uncertainty_percent": mobility.get(
                        "forecast_uncertainty_percent"
                    ),
                }
            if entity_key.startswith("thermal_loss_") or entity_key in {
                "circulation_loss",
                "storage_standby_loss",
                "storage_heat_loss_coefficient",
            }:
                snapshot_attributes[entity_key] |= {
                    "estimated_value": True,
                    "control_services_called": False,
                    "explanation": thermal_loss.get("explanation"),
                    "evidence": thermal_loss.get("evidence", []),
                    "passive_cooling_intervals": thermal_loss.get(
                        "passive_cooling_intervals"
                    ),
                    "storage_temperature_c": thermal_loss.get("storage_temperature_c"),
                    "ambient_temperature_c": thermal_loss.get("ambient_temperature_c"),
                    "outdoor_response_factor": thermal_loss.get(
                        "outdoor_response_factor"
                    ),
                }
        return RecommendationSnapshot(snapshot_values, snapshot_attributes)

    @staticmethod
    def _recommendation_explanation(
        *,
        action: str,
        reason: str,
        surplus: float,
        confidence: int,
        uncertainty: float | None,
        price: float | None,
        threshold: float,
        candidate: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if reason == "data_unavailable":
            explanation_headline = "Noch keine belastbare Empfehlung"
            explanation_summary = "Prognose- oder Qualitätsdaten reichen für eine begründete Empfehlung noch nicht aus."
        elif action == "heating":
            explanation_headline = "Komfortbedarf hat Vorrang"
            explanation_summary = "Die gemessene Innentemperatur liegt unter dem Sollwert; deshalb wird Heizen empfohlen."
        elif action == "dhw" and reason == "pv_surplus":
            explanation_headline = "PV-Fenster für Warmwasser erkannt"
            explanation_summary = "Nach Hausbedarf und weiteren Reserven bleibt voraussichtlich genügend PV-Energie für Warmwasser."
        elif action == "thermal_storage" and reason == "pv_surplus":
            explanation_headline = "PV-Fenster für den Wärmespeicher erkannt"
            explanation_summary = "Der verbleibende PV-Überschuss kann bevorzugt thermisch genutzt werden."
        elif reason == "low_price":
            explanation_headline = "Günstiges Tarifzeitfenster erkannt"
            explanation_summary = "Der aktuelle Strompreis liegt unter der konfigurierten Niedrigpreisgrenze."
        elif reason == "dhw_max_reached":
            explanation_headline = "Warmwassergrenze erreicht"
            explanation_summary = "Die konfigurierte Warmwasser-Höchsttemperatur ist erreicht; eine weitere Anhebung wird nicht empfohlen."
        elif reason == "dhw_limit_quantity_mismatch":
            explanation_headline = "Warmwassergrenze passt nicht zur Messgröße"
            explanation_summary = "Die konfigurierte Grenze gilt für eine andere Temperatur als der zugeordnete Fühler misst. Bis Zapf- und Speichergrenze zur Sensorzuordnung passen, wird keine Warmwasserempfehlung ausgesprochen."
        elif reason in {
            "dhw_disabled",
            "dhw_unanswered",
            "dhw_limit_unanswered",
            "hydraulics_setup_incomplete",
        }:
            explanation_headline = "Warmwasserempfehlung gesperrt"
            explanation_summary = "Die Hydraulik- oder Warmwassergrenzen sind nicht vollständig eingerichtet; deshalb gibt es keine Warmwasserempfehlung."
        elif action == "defer":
            explanation_headline = "Verschieben kann Kosten vermeiden"
            explanation_summary = "Der Strompreis liegt über der Grenze und der Kompressor ist aktuell nicht aktiv."
        else:
            explanation_headline = "Aktuell keine Aktion erforderlich"
            explanation_summary = "EAI erkennt derzeit kein ausreichend belastbares Energie- oder Komfortsignal."
        explanation_evidence = []
        if candidate:
            window_timestamp = candidate.get("timestamp")
            if window_timestamp:
                explanation_evidence.append(
                    f"Bewertetes Zeitfenster: {window_timestamp}"
                )
            forecast_kwh = candidate.get("forecast_kw")
            forecast_lower_kwh = candidate.get("lower_kw")
            forecast_upper_kwh = candidate.get("upper_kw")
            if all(
                (
                    isinstance(forecast_band_value, (int, float))
                    for forecast_band_value in (
                        forecast_kwh,
                        forecast_lower_kwh,
                        forecast_upper_kwh,
                    )
                )
            ):
                explanation_evidence.append(
                    f"Wärmepumpenbedarf {float(forecast_kwh):.2f} kWh, Prognoseband {float(forecast_lower_kwh):.2f}–{float(forecast_upper_kwh):.2f} kWh"
                )
        if surplus > 0:
            explanation_evidence.append(
                f"Erwarteter verbleibender PV-Überschuss {surplus:.2f} kWh"
            )
        if price is not None:
            explanation_evidence.append(
                f"Strompreis {price:.1f} ct/kWh bei Grenze {threshold:.1f} ct/kWh"
            )
        return {
            "explanation_headline": explanation_headline,
            "explanation_summary": explanation_summary,
            "explanation_evidence": explanation_evidence,
            "confidence_percent": confidence,
            "uncertainty_percent": uncertainty,
            "summary": explanation_summary,
        }

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return

    def _state_number(
        self, entity_id: str | None, *, climate_attribute: str | None = None
    ) -> float | None:
        if not entity_id or (entity_state := self.hass.states.get(entity_id)) is None:
            return None
        state_value = entity_state.state
        if entity_id.startswith("climate.") and climate_attribute:
            state_value = (entity_state.attributes or {}).get(climate_attribute)
        return self._number(state_value)

    @staticmethod
    def _datetime(value: Any) -> datetime | None:
        if not value:
            return
        try:
            parsed_datetime = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return
        return (
            parsed_datetime.replace(tzinfo=timezone.utc)
            if parsed_datetime.tzinfo is None
            else parsed_datetime.astimezone(timezone.utc)
        )

    def _price_ct(
        self, entity_id: str | None, configured_unit: str | None = None
    ) -> float | None:
        if not entity_id or (entity_state := self.hass.states.get(entity_id)) is None:
            return None
        price_value = self._number(entity_state.state)
        if price_value is None:
            return
        price_unit = str(
            (entity_state.attributes or {}).get("unit_of_measurement") or ""
        ).lower()
        convert_eur_to_ct = configured_unit == "eur_per_kwh" or (
            configured_unit in (None, "auto")
            and ("eur" in price_unit or "€/" in price_unit)
        )
        return round(price_value * 100, 3) if convert_eur_to_ct else price_value

    @staticmethod
    def _point_time(point: dict[str, Any] | None) -> datetime | None:
        if not point or not point.get("timestamp"):
            return
        try:
            point_datetime = datetime.fromisoformat(
                str(point["timestamp"]).replace("Z", "+00:00")
            )
            return (
                point_datetime.replace(tzinfo=timezone.utc)
                if point_datetime.tzinfo is None
                else point_datetime.astimezone(timezone.utc)
            )
        except ValueError:
            return

    def _forecast_windows(
        self, hours: list[dict[str, Any]], now: datetime
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        timed_windows = [
            (point_datetime, forecast_point)
            for forecast_point in hours
            if (point_datetime := self._point_time(forecast_point))
            if point_datetime + timedelta(hours=1) > now
        ]
        timed_windows.sort(key=lambda item: item[0])
        current_window = next(
            (
                forecast_point
                for point_datetime, forecast_point in timed_windows
                if point_datetime <= now < point_datetime + timedelta(hours=1)
            ),
            None,
        )
        best_window = max(
            timed_windows[:24],
            key=lambda item: self._surplus(item[1]),
            default=(None, None),
        )[1]
        return (current_window, best_window)

    @staticmethod
    def _surplus(point: dict[str, Any] | None) -> float:
        if not point:
            return 0.0
        pv_after_household_kwh = point.get("pv_after_household_kwh")
        if not isinstance(pv_after_household_kwh, (int, float)):
            pv_forecast_kwh = point.get("pv_forecast_kwh")
            household_base_load_kwh = point.get("household_base_load_kwh")
            pv_after_household_kwh = (
                max(0.0, float(pv_forecast_kwh) - float(household_base_load_kwh))
                if isinstance(pv_forecast_kwh, (int, float))
                and isinstance(household_base_load_kwh, (int, float))
                else pv_forecast_kwh
            )
        heat_pump_forecast_kwh = point.get("forecast_kw")
        return (
            float(pv_after_household_kwh) - float(heat_pump_forecast_kwh)
            if isinstance(pv_after_household_kwh, (int, float))
            and isinstance(heat_pump_forecast_kwh, (int, float))
            else 0.0
        )
