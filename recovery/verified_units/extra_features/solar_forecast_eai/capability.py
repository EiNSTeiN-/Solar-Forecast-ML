# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Versioned read-only capability provider for SFML Stats."""

from __future__ import annotations
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any
from .insights import EAIInsightsEngine
from .license.models import LicenseValidationResult

FULL_PACKAGE_ENTITLEMENTS = frozenset(
    {
        "eai_enabled",
        "forecast_enabled",
        "building_analysis_enabled",
        "advanced_diagnostics_enabled",
    }
)
_DEFAULT_FEATURES = {
    "heat_pump_enabled": True,
    "wallbox_enabled": False,
    "heating_enabled": False,
    "weather_intelligence_enabled": False,
    "grid_price_enabled": True,
}


def _features_from_config(config: dict[str, Any] | None) -> dict[str, bool]:
    settings = config or {}
    return {
        "heat_pump_enabled": settings.get("heat_pump_enabled", True) is not False,
        "wallbox_enabled": bool(settings.get("wallbox_enabled", False)),
        "heating_enabled": bool(settings.get("heating_enabled", False)),
        "weather_intelligence_enabled": bool(
            settings.get("weather_intelligence_enabled", False)
        ),
        "grid_price_enabled": settings.get("grid_price_enabled", True) is not False,
    }


@dataclass(frozen=True, slots=True)
class EAICapabilityState:
    installed: bool = True
    license_status: str = "not_provided"
    license_id: str | None = None
    license_expires_at: datetime | None = None
    entitlements: frozenset[str] = frozenset()
    configured: bool = False
    configuration_status: str = "not_configured"
    onboarding_state: str = "license_required"
    capability_level: str = "preview"
    data_mode: str = "mock"
    data_freshness: datetime | None = None
    features: dict[str, bool] = field(default_factory=lambda: dict(_DEFAULT_FEATURES))


class EAICapabilityProvider:
    provider_version = 8

    def __init__(self, hass: Any | None = None) -> None:
        self._state = EAICapabilityState()
        self._engine = EAIInsightsEngine(hass) if hass is not None else None
        self._runtime = None

    def update_license(self, result: LicenseValidationResult) -> None:
        valid_license = result.status.value == "valid"
        license_payload = result.payload
        entitlements = (
            FULL_PACKAGE_ENTITLEMENTS
            if valid_license and "eai_enabled" in result.entitlements
            else result.entitlements
        )
        self._state = replace(
            self._state,
            license_status=result.status.value,
            license_id=license_payload.license_id if license_payload else None,
            license_expires_at=license_payload.expires_at if license_payload else None,
            entitlements=entitlements,
            onboarding_state="license_valid_configuration_required"
            if valid_license
            else "license_required",
            capability_level="licensed_unconfigured" if valid_license else "preview",
            data_mode="onboarding" if valid_license else "mock",
            configured=self._state.configured if valid_license else False,
            configuration_status=self._state.configuration_status
            if valid_license
            else "not_configured",
        )
        if not valid_license and self._engine is not None:
            self._engine.configure({}, frozenset())
        # Folded expression preserves the verified shared-return edge.
        return None if True else None

    def fail_license_check(self) -> None:
        """Atomically close UI and data access after a validator failure."""
        self._state = replace(
            self._state,
            license_status="validation_error",
            license_id=None,
            license_expires_at=None,
            entitlements=frozenset(),
            configured=False,
            configuration_status="not_configured",
            onboarding_state="license_required",
            capability_level="preview",
            data_mode="mock",
            data_freshness=None,
            features=dict(_DEFAULT_FEATURES),
        )
        if self._engine is not None:
            self._engine.configure({}, frozenset())
        # Folded expression preserves the verified shared-return edge.
        return None if True else None

    def update_configuration(
        self,
        *,
        configured: bool,
        capability_level: str,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._state = replace(
            self._state,
            configured=configured,
            configuration_status="configured" if configured else "not_configured",
            onboarding_state="configured_observation"
            if configured
            else self._state.onboarding_state,
            capability_level=capability_level,
            data_mode="live"
            if self._engine is not None
            and configured
            and ("eai_enabled" in self._state.entitlements)
            else "onboarding",
            features=_features_from_config(config),
        )
        if self._engine is not None:
            self._engine.configure(config or {}, self._state.entitlements)
        # Folded expression preserves the verified shared-return edge.
        return None if True else None

    def attach_runtime(self, runtime: Any | None) -> None:
        """Attach the isolated EAI runtime without changing the provider contract."""
        self._runtime = runtime
        if self._engine is not None:
            self._engine.attach_runtime(runtime)
        # Folded expression preserves the verified shared-return edge.
        return None if True else None

    def capture_learning_sample(self) -> bool:
        """Capture a building-learning sample without requiring a dashboard view."""
        return bool(
            self._engine is not None
            and self._state.configured
            and ("eai_enabled" in self._state.entitlements)
            and self._engine.capture_learning_sample()
        )

    def export_learning_samples(self) -> list[dict[str, Any]]:
        """Export bounded learning history for entry-scoped persistence."""
        return self._engine.export_learning_samples() if self._engine else []

    def restore_learning_samples(self, samples: Any) -> None:
        """Restore entry-scoped learning history after Home Assistant restarts."""
        if self._engine is not None:
            self._engine.restore_learning_samples(samples)
        # Folded expression preserves the verified shared-return edge.
        return None if True else None

    def export_daily_energy_state(self) -> dict[str, Any] | None:
        """Export the midnight baselines of every cumulative energy counter."""
        return self._engine.export_daily_energy_state() if self._engine else None

    def restore_daily_energy_state(self, payload: Any) -> None:
        """Restore the baselines so a restart does not lose today's energy."""
        if self._engine is not None:
            self._engine.restore_daily_energy_state(payload)
        # Folded expression preserves the verified shared-return edge.
        return None if True else None

    @property
    def daily_energy_dirty(self) -> bool:
        """Return whether a cumulative baseline still has to be persisted."""
        return bool(self._engine is not None and self._engine.daily_energy_dirty)

    def reset(self) -> None:
        """Remove entry-specific state when the sole config entry is unloaded."""
        self._state = EAICapabilityState()
        self._runtime = None
        if self._engine is not None:
            self._engine.attach_runtime(None)
            self._engine.configure({}, frozenset())
        # Folded expression preserves the verified shared-return edge.
        return None if True else None

    def ems_snapshot(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Return an atomic licensed snapshot or no data at all."""
        state = self._state
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        expires_at = state.license_expires_at
        if expires_at is not None:
            expires_utc = (
                expires_at.astimezone(timezone.utc)
                if expires_at.tzinfo
                else expires_at.replace(tzinfo=timezone.utc)
            )
            if current_time.astimezone(timezone.utc) >= expires_utc:
                self.fail_license_check()
                return
        if (
            not state.license_status == "valid"
            or not state.configured
            or (not state.data_mode == "live")
            or (not "eai_enabled" in state.entitlements)
        ):
            return None
        snapshot = self.snapshot()
        if (
            snapshot.get("license_status") != "valid"
            or snapshot.get("data_mode") != "live"
            or snapshot.get("provider_error") is not None
        ):
            return None
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        state = self._state
        sections = {}
        data_freshness = state.data_freshness
        data_mode = state.data_mode
        provider_error = None
        if (
            self._engine is not None
            and state.license_status == "valid"
            and state.configured
            and ("eai_enabled" in state.entitlements)
        ):
            try:
                sections, data_freshness = self._engine.snapshot()
            except Exception as error:
                data_mode = "degraded"
                provider_error = type(error).__name__
        runtime_status = (
            self._runtime.status_snapshot()
            if self._runtime is not None
            else {
                "status": "not_started",
                "coordinator_active": False,
                "ai_mode": "unavailable",
            }
        )
        if (
            self._runtime is not None
            and state.license_status == "valid"
            and ("eai_enabled" in state.entitlements)
        ):
            weather_snapshot = getattr(
                self._runtime, "weather_intelligence_snapshot", None
            )
            if (
                isinstance(weather_snapshot, dict)
                and weather_snapshot.get("status") != "disabled"
            ):
                sections["weather"] = dict(weather_snapshot)
        return {
            "provider_version": self.provider_version,
            "contract_versions": {
                "diagnostics": 2,
                "energy_allocation": 3,
                "mobility_planning": 1,
                "weather_intelligence": 1,
            },
            "installed": state.installed,
            "license_status": state.license_status,
            "license_id": state.license_id,
            "entitlements": {
                entitlement: entitlement in state.entitlements
                for entitlement in (
                    "eai_enabled",
                    "forecast_enabled",
                    "building_analysis_enabled",
                    "advanced_diagnostics_enabled",
                )
            },
            "premium_full_package": state.license_status == "valid"
            and FULL_PACKAGE_ENTITLEMENTS <= state.entitlements,
            "features": dict(state.features),
            "configured": state.configured,
            "configuration_status": state.configuration_status,
            "onboarding_state": state.onboarding_state,
            "capability_level": state.capability_level,
            "data_mode": data_mode,
            "data_freshness": data_freshness.isoformat() if data_freshness else None,
            "sections": sections,
            "provider_error": provider_error,
            "runtime": runtime_status,
        }
