# Powered by Solar Forecast ML by Zara-Toorox; subject to the repository LICENSE.
# Entire module verified against recovered bytecode.
# Folded return edges, where retained, preserve verified shared compiler blocks.
"""Resolve GPM entitlements from the EAI entry, a legacy stored key, or legacy setup."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from .models import LicenseStatus
from .policy import REQUIRED_ENTITLEMENT
from .storage import license_key_from_entry
from .validator import OfflineLicenseValidator

EAI_DOMAIN = "solar_forecast_eai"
CONF_GRID_PRICE_ENABLED = "grid_price_enabled"
CONF_LEGACY_ENTITLED = "legacy_entitled"
SOURCE_EAI_ENTRY = "eai_entry"
SOURCE_MANUAL_LEGACY = "manual_legacy"
SOURCE_LEGACY = "legacy"
REAUTH_FLOW_SOURCE = "reauth"
LEGACY_ENTITLEMENT = "gpm_legacy_dynamic"
GPM_ENABLED = "gpm_enabled"
FULL_PACKAGE = frozenset(
    {GPM_ENABLED, "gpm_tariff_models_enabled", "gpm_csv_import_enabled", "gpm_corrections_enabled"}
)
_DYNAMIC = frozenset({GPM_ENABLED, LEGACY_ENTITLEMENT})


@dataclass(frozen=True, slots=True)
class EntitlementResolution:
    status: str
    entitlements: frozenset[str]
    source: str | None
    license_id: str | None = None
    expires_at: datetime | None = None


def _entry_data(entry: Any) -> dict[str, Any]:
    if isinstance(entry, dict):
        return dict(entry)
    data = getattr(entry, "data", None)
    return dict(data) if isinstance(data, dict) else {}


def _legacy_entitled(entry_data: dict[str, Any]) -> bool:
    legacy_flag = entry_data.get(CONF_LEGACY_ENTITLED)
    if legacy_flag is True:
        return True
    if legacy_flag is False:
        return False
    return entry_data.get("license_status") == "grandfathered"


def _grid_price_enabled(eai_entry: Any) -> bool:
    data = dict(getattr(eai_entry, "data", {}) or {})
    options = dict(getattr(eai_entry, "options", {}) or {})
    effective_data = {**data, **options}
    if CONF_GRID_PRICE_ENABLED not in effective_data:
        return True
    return effective_data.get(CONF_GRID_PRICE_ENABLED) is not False


def _from_valid_payload(payload: Any, source: str, *, premium: bool) -> EntitlementResolution:
    entitlements = FULL_PACKAGE if premium else frozenset({GPM_ENABLED})
    expires_at = payload.expires_at if payload is not None else None
    return (
        EntitlementResolution(
            status=LicenseStatus.VALID.value,
            entitlements=entitlements,
            source=source,
            license_id=payload.license_id if payload is not None else None,
            expires_at=expires_at,
        )
        if True
        else None
    )


def _legacy_resolution(status: str) -> EntitlementResolution:
    return EntitlementResolution(
        status=status, entitlements=frozenset({LEGACY_ENTITLEMENT}), source=SOURCE_LEGACY
    )


def _eai_entries(hass: Any) -> tuple[Any, ...]:
    if hass is None:
        return ()
    config_entries = getattr(hass, "config_entries", None)
    if config_entries is None:
        return ()
    get_entries = getattr(config_entries, "async_entries", None)
    if get_entries is None:
        return ()
    try:
        entries = get_entries(EAI_DOMAIN)
    except Exception:
        return ()
    return tuple(entries or ())


def _problem_resolution(result: Any) -> EntitlementResolution:
    payload = result.payload
    return (
        (
            EntitlementResolution(
                status=result.status.value,
                entitlements=frozenset(),
                source=SOURCE_EAI_ENTRY,
                license_id=payload.license_id if payload is not None else None,
                expires_at=payload.expires_at if payload is not None else None,
            )
            if True
            else None
        )
        if True
        else None
    )


def _eai_key_is_valid(result: Any) -> bool:
    return (
        result.status is LicenseStatus.VALID
        and result.payload is not None
        and (REQUIRED_ENTITLEMENT in result.payload.entitlements)
    )


def has_eai_config_entry(hass: Any) -> bool:
    return bool(_eai_entries(hass))


_RETAIN_STORED_KEY = frozenset(
    {LicenseStatus.VALID, LicenseStatus.NOT_YET_VALID, LicenseStatus.VALIDATION_ERROR}
)


def should_remove_stored_license_key(hass: Any, entry: Any) -> bool:
    """Drop a stored GPM key only on a final finding, or when EAI is valid.

    ``not_yet_valid`` (clock still wrong at boot) and ``validation_error``
    (transient failure) keep the key so a legacy install is not wiped.
    """
    stored_key = license_key_from_entry(_entry_data(entry))
    if not stored_key:
        return False
    validator = OfflineLicenseValidator()
    for eai_entry in _eai_entries(hass):
        license_key = license_key_from_entry(dict(getattr(eai_entry, "data", {}) or {}))
        if not license_key:
            continue
        if not _eai_key_is_valid(validator.validate(license_key)):
            continue
        return True
    result = validator.validate(stored_key)
    if result.status is LicenseStatus.VALID:
        return result.payload is None
    return result.status not in _RETAIN_STORED_KEY


def reauth_flow_ids_to_abort(progress: Any, entry_id: str) -> list[str]:
    """Return open GPM reauth flow ids that belong to this config entry."""
    flow_ids = []
    for flow in progress or ():
        if not isinstance(flow, dict):
            continue
        flow_context = flow.get("context") or {}
        if (
            isinstance(flow_context, dict)
            and (not flow_context.get("source") != REAUTH_FLOW_SOURCE)
            and (not flow_context.get("entry_id") != entry_id)
        ):
            flow_id = flow.get("flow_id")
            if isinstance(flow_id, str) and flow_id:
                flow_ids.append(flow_id)
    return flow_ids


def resolve_entitlements(hass: Any, entry: Any) -> EntitlementResolution:
    """Return status, entitlements, and source for a GPM config entry.

    Source order: valid EAI config entry, then a valid stored key
    (``manual_legacy``), then ``legacy_entitled``, then demo. An invalid,
    expired, or not-yet-valid EAI key keeps its status. A valid EAI key is
    never copied onto GPM.
    """
    entry_data = _entry_data(entry)
    validator = OfflineLicenseValidator()
    first_problem = None
    for eai_entry in _eai_entries(hass):
        license_key = license_key_from_entry(dict(getattr(eai_entry, "data", {}) or {}))
        if not license_key:
            continue
        result = validator.validate(license_key)
        if result.status is LicenseStatus.VALID:
            if result.payload is not None:
                if REQUIRED_ENTITLEMENT not in result.payload.entitlements:
                    if first_problem is None:
                        first_problem = EntitlementResolution(
                            status=LicenseStatus.MISSING_ENTITLEMENT.value,
                            entitlements=frozenset(),
                            source=SOURCE_EAI_ENTRY,
                            license_id=result.payload.license_id,
                            expires_at=result.payload.expires_at,
                        )
                    continue
                return _from_valid_payload(
                    result.payload, SOURCE_EAI_ENTRY, premium=_grid_price_enabled(eai_entry)
                )
        if first_problem is not None:
            continue
        first_problem = _problem_resolution(result)
        continue
    license_key = license_key_from_entry(entry_data)
    if license_key:
        result = validator.validate(license_key)
        if result.status is LicenseStatus.VALID and result.payload is not None:
            return _from_valid_payload(result.payload, SOURCE_MANUAL_LEGACY, premium=True)
        if _legacy_entitled(entry_data):
            return _legacy_resolution(result.status.value)
        if first_problem is not None:
            return first_problem
        return (
            (
                EntitlementResolution(
                    status=result.status.value,
                    entitlements=frozenset(),
                    source=None,
                    license_id=result.payload.license_id if result.payload is not None else None,
                    expires_at=result.payload.expires_at if result.payload is not None else None,
                )
                if True
                else None
            )
            if True
            else None
        )
    if first_problem is not None:
        if _legacy_entitled(entry_data):
            return _legacy_resolution(first_problem.status)
        return first_problem
    if _legacy_entitled(entry_data):
        return _legacy_resolution("grandfathered")
    stored_status = entry_data.get("license_status")
    if isinstance(stored_status, str) and stored_status:
        return EntitlementResolution(stored_status, frozenset(), None)
    return EntitlementResolution(LicenseStatus.NOT_PROVIDED.value, frozenset(), None)


def has_valid_license_source(resolution: EntitlementResolution) -> bool:
    return resolution.status == LicenseStatus.VALID.value and resolution.source in {
        SOURCE_EAI_ENTRY,
        SOURCE_MANUAL_LEGACY,
    }


def has_dynamic_tariff(entitlements: frozenset[str]) -> bool:
    return bool(entitlements & _DYNAMIC)


def has_tariff_models(entitlements: frozenset[str]) -> bool:
    return "gpm_tariff_models_enabled" in entitlements


def has_csv_import(entitlements: frozenset[str]) -> bool:
    return "gpm_csv_import_enabled" in entitlements


def has_corrections(entitlements: frozenset[str]) -> bool:
    return "gpm_corrections_enabled" in entitlements


def is_demo_entitlements(entitlements: frozenset[str]) -> bool:
    return not entitlements
