# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Signed, installation-bound DEV-mode token. @zara"""
from __future__ import annotations
import base64
import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
import voluptuous as vol
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_LOGGER = logging.getLogger(__name__)
DOMAIN = "solar_forecast_ml"
DEV_MODE_KEY = "dev_mode"
SERVICE_SET_DEV_TOKEN = "set_dev_token"
SERVICE_CLEAR_DEV_TOKEN = "clear_dev_token"
STORE_VERSION = 1
STORE_KEY = "solar_forecast_ml.dev_mode"
ENTITLEMENT = "sfml_dev_mode"
PREFIX = "SFMLDEV1"
SIGNING_CONTEXT = b"solar_forecast_ml-dev-mode-v1\x00"
INSTALL_HASH_PREFIX = b"sfml-dev-install:v1:"
MAX_KEY_LENGTH = 4096
MAX_PAYLOAD_BYTES = 2048
MAX_STRING_LENGTH = 256
_REQUIRED = frozenset(
    {"key_id", "issued_at", "expires_at", "entitlement", "install_hash"}
)


class DevTokenStatus(StrEnum):
    """Outcome of one token check. Never carries secret material."""

    NOT_PROVIDED = "not_provided"
    VALID = "valid"
    INVALID_FORMAT = "invalid_format"
    INVALID_SIGNATURE = "invalid_signature"
    UNKNOWN_KEY_ID = "unknown_key_id"
    WRONG_ENTITLEMENT = "wrong_entitlement"
    INSTALL_MISMATCH = "install_mismatch"
    NOT_YET_VALID = "not_yet_valid"
    EXPIRED = "expired"
    VALIDATION_ERROR = "validation_error"


PRODUCTION_PUBLIC_KEYS: dict[str, bytes] = {
    "sfml-dev-2026-01": bytes.fromhex(
        "03b9800992f3d5b70a657314e6f3bd238ed629118a6f983c722625676ad7ebdf"
    )
}


@dataclass(frozen=True, slots=True)
class DevTokenResult:
    """Validator result. The raw token is never stored here."""

    status: DevTokenStatus
    expires_at: datetime | None = None
    install_hash: str = ""


def _inactive(status: DevTokenStatus, install_hash: str = "") -> dict[str, Any]:
    return {
        "active": False,
        "expires_at": None,
        "install_hash": install_hash,
        "status": status.value,
    }


DEV_ONLY_UNIQUE_ID_MARKERS: tuple[str, ...] = (
    "ml_ai_rmse",
    "active_prediction_model",
    "ml_last_ai_training",
    "physics_samples",
    "drift_status",
    "ml_workflow_status",
    "ml_coordinator_health",
    "data_files_status",
    "ml_eod_duration",
    "ml_last_coordinator_update",
    "ml_next_scheduled_update",
    "ml_cloudiness_trend_1h",
    "ml_cloudiness_trend_3h",
    "ml_cloudiness_volatility",
    "ml_yesterday_deviation",
    "ml_evaluation_coverage_7d",
    "ml_evaluation_coverage_30d",
    "ml_excluded_mppt_hours_7d",
    "ml_excluded_mppt_hours_30d",
    "ml_avg_accuracy_30d",
    "ml_hybrid_forecast_status",
    "external_sensors_status",
    "ml_service_status",
    "ml_training_readiness",
    "pattern_count",
    "diagnostic_status",
)


def unique_id_requires_dev_mode(unique_id: Any) -> bool:
    """True when this entity exists only while DEV mode is active."""
    normalized_unique_id = str(unique_id or "").lower()
    return any(
        (
            dev_only_marker in normalized_unique_id
            for dev_only_marker in DEV_ONLY_UNIQUE_ID_MARKERS
        )
    )


def get_dev_mode(hass: Any) -> dict[str, Any]:
    """Return the published DEV-mode contract. Never raises."""
    try:
        domain_data = hass.data.get(DOMAIN, {})
        dev_mode_state = (
            domain_data.get(DEV_MODE_KEY) if isinstance(domain_data, dict) else None
        )
        if isinstance(dev_mode_state, dict):
            return {
                "active": bool(dev_mode_state.get("active")),
                "expires_at": dev_mode_state.get("expires_at"),
                "install_hash": str(dev_mode_state.get("install_hash") or ""),
                "status": str(
                    dev_mode_state.get("status") or DevTokenStatus.NOT_PROVIDED.value
                ),
            }
    except Exception:
        _LOGGER.debug("DEV mode state unreadable")
    return _inactive(DevTokenStatus.NOT_PROVIDED)


def canonical_payload(payload: Mapping[str, Any]) -> bytes:
    """Stable JSON bytes that the signature covers."""
    return json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def encode_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode_segment(value: str, *, maximum: int) -> bytes:
    if not value or any(
        (
            character
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        )
    ):
        raise ValueError("invalid_base64url")
    base64_padding = "=" * (-len(value) % 4)
    decoded_bytes = base64.b64decode(
        value + base64_padding, altchars=b"-_", validate=True
    )
    if encode_segment(decoded_bytes) != value:
        raise ValueError("non_canonical_base64url")
    if len(decoded_bytes) > maximum:
        raise ValueError("segment_too_large")
    return decoded_bytes


def compose_token(payload: Mapping[str, Any], signature: bytes) -> str:
    payload_bytes = canonical_payload(payload)
    if len(payload_bytes) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload_too_large")
    if len(signature) != 64:
        raise ValueError("signature_length")
    return f"{PREFIX}.{encode_segment(payload_bytes)}.{encode_segment(signature)}"


def _utc_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 40 or (not value.endswith("Z")):
        raise ValueError("timestamp")
    parsed_timestamp = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed_timestamp.tzinfo is None:
        raise ValueError("timestamp")
    return parsed_timestamp.astimezone(timezone.utc)


def _bounded_string(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_STRING_LENGTH:
        raise ValueError("string")
    return value


class DevTokenValidator:
    """Offline Ed25519 check. An empty key registry rejects every token."""

    def __init__(self, public_keys: Mapping[str, bytes] | None = None) -> None:
        key_registry = PRODUCTION_PUBLIC_KEYS if public_keys is None else public_keys
        self._public_keys = dict(key_registry)

    def validate(
        self, token: str | None, *, install_hash: str, now: datetime | None = None
    ) -> DevTokenResult:
        if token is None:
            return DevTokenResult(DevTokenStatus.NOT_PROVIDED)
        if not isinstance(token, str):
            return DevTokenResult(DevTokenStatus.INVALID_FORMAT)
        token = token.strip()
        if not token:
            return DevTokenResult(DevTokenStatus.NOT_PROVIDED)
        if len(token) > MAX_KEY_LENGTH:
            return DevTokenResult(DevTokenStatus.INVALID_FORMAT)
        try:
            token_segments = token.split(".")
            if len(token_segments) != 3 or token_segments[0] != PREFIX:
                return DevTokenResult(DevTokenStatus.INVALID_FORMAT)
            payload_bytes = decode_segment(token_segments[1], maximum=MAX_PAYLOAD_BYTES)
            signature_bytes = decode_segment(token_segments[2], maximum=64)
            if len(signature_bytes) != 64:
                return DevTokenResult(DevTokenStatus.INVALID_FORMAT)
            payload = json.loads(payload_bytes.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != _REQUIRED:
                return DevTokenResult(DevTokenStatus.INVALID_FORMAT)
            key_id = _bounded_string(payload["key_id"])
            entitlement = _bounded_string(payload["entitlement"])
            payload_install_hash = _bounded_string(payload["install_hash"])
            if len(payload_install_hash) != 16 or any(
                (
                    character not in "0123456789abcdef"
                    for character in payload_install_hash
                )
            ):
                return DevTokenResult(DevTokenStatus.INVALID_FORMAT)
            issued_at = _utc_timestamp(payload["issued_at"])
            expires_at = _utc_timestamp(payload["expires_at"])
            canonical_bytes = canonical_payload(payload)
            if canonical_bytes != payload_bytes:
                return DevTokenResult(DevTokenStatus.INVALID_FORMAT)
            public_key_bytes = self._public_keys.get(key_id)
            if public_key_bytes is None:
                return DevTokenResult(DevTokenStatus.UNKNOWN_KEY_ID)
            Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
                signature_bytes, SIGNING_CONTEXT + canonical_bytes
            )
        except InvalidSignature:
            return DevTokenResult(DevTokenStatus.INVALID_SIGNATURE)
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return DevTokenResult(DevTokenStatus.INVALID_FORMAT)
        except Exception:
            return DevTokenResult(DevTokenStatus.VALIDATION_ERROR)
        if entitlement != ENTITLEMENT:
            return DevTokenResult(DevTokenStatus.WRONG_ENTITLEMENT)
        if payload_install_hash != install_hash:
            return DevTokenResult(DevTokenStatus.INSTALL_MISMATCH)
        now_utc = now or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        now_utc = now_utc.astimezone(timezone.utc)
        if issued_at > now_utc:
            return DevTokenResult(DevTokenStatus.NOT_YET_VALID)
        if expires_at <= now_utc:
            return DevTokenResult(DevTokenStatus.EXPIRED, expires_at=expires_at)
        return DevTokenResult(
            DevTokenStatus.VALID,
            expires_at=expires_at,
            install_hash=payload_install_hash,
        )


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def async_install_hash(hass: Any) -> str:
    """Truncated install binding. The raw instance id is not logged."""
    from homeassistant.helpers.instance_id import async_get as async_get_instance_id

    instance_id = await async_get_instance_id(hass)
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("instance_id")
    install_digest = hashlib.sha256(INSTALL_HASH_PREFIX + instance_id.encode("utf-8"))
    return install_digest.hexdigest()[:16]


class _DevTokenStore:

    def __init__(self, hass: Any) -> None:
        from homeassistant.helpers.storage import Store as Store

        self._store = Store(hass, STORE_VERSION, STORE_KEY, private=True)

    async def async_load(self) -> str | None:
        stored_token = await self._store.async_load()
        if isinstance(stored_token, str) and stored_token:
            return stored_token
        return None if True else None

    async def async_save(self, token: str) -> None:
        await self._store.async_save(token)

    async def async_clear(self) -> None:
        await self._store.async_remove()


def _publish(hass: Any, state: dict[str, Any]) -> dict[str, Any]:
    try:
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN][DEV_MODE_KEY] = state
    except Exception:
        _LOGGER.debug("DEV mode state not published")
    try:
        from .core_user_messages import (
            set_dev_message_visibility as set_dev_message_visibility,
        )

        set_dev_message_visibility(bool(state.get("active")))
    except Exception:
        _LOGGER.debug("DEV mode message visibility not published")
    return state if True else None


async def async_evaluate_dev_mode(hass: Any) -> dict[str, Any]:
    """Load and check the stored token. Does not log the token."""
    install_hash = ""
    try:
        install_hash = await async_install_hash(hass)
        stored_token = await _DevTokenStore(hass).async_load()
        if not stored_token:
            return _publish(hass, _inactive(DevTokenStatus.NOT_PROVIDED, install_hash))
        validation_result = DevTokenValidator().validate(
            stored_token, install_hash=install_hash
        )
    except Exception:
        _LOGGER.info("DEV mode inactive (%s)", DevTokenStatus.VALIDATION_ERROR.value)
        _cancel_expiry_listener(hass)
        return _publish(hass, _inactive(DevTokenStatus.VALIDATION_ERROR, install_hash))
    if (
        validation_result.status is not DevTokenStatus.VALID
        or validation_result.expires_at is None
    ):
        if validation_result.status is not DevTokenStatus.NOT_PROVIDED:
            _LOGGER.info("DEV mode inactive (%s)", validation_result.status.value)
        _cancel_expiry_listener(hass)
        return _publish(hass, _inactive(validation_result.status, install_hash))
    published_state = _publish(
        hass,
        {
            "active": True,
            "expires_at": _iso_z(validation_result.expires_at),
            "install_hash": install_hash,
            "status": DevTokenStatus.VALID.value,
        },
    )
    _arm_expiry_listener(hass, validation_result.expires_at, install_hash)
    return published_state


async def async_set_dev_token(hass: Any, token: Any) -> DevTokenStatus:
    """Validate immediately and store only a valid token string."""
    try:
        install_hash = await async_install_hash(hass)
        validation_result = DevTokenValidator().validate(
            token if isinstance(token, str) else None, install_hash=install_hash
        )
        if validation_result.status is DevTokenStatus.VALID:
            await _DevTokenStore(hass).async_save(str(token).strip())
    except Exception:
        validation_result = DevTokenResult(DevTokenStatus.VALIDATION_ERROR)
    if validation_result.status is DevTokenStatus.VALID:
        _LOGGER.info("DEV token stored (valid); takes effect after the next restart")
    else:
        _LOGGER.info("DEV token rejected (%s)", validation_result.status.value)
    return validation_result.status if True else None


async def async_clear_dev_token(hass: Any) -> None:
    try:
        await _DevTokenStore(hass).async_clear()
    except Exception:
        _LOGGER.info("DEV token clear failed; takes effect after the next restart")
        return
    _LOGGER.info("DEV token cleared; takes effect after the next restart")


def _cancel_expiry_listener(hass: Any) -> None:
    try:
        domain_data = hass.data.get(DOMAIN)
        if not isinstance(domain_data, dict):
            return
        unsubscribe_expiry = domain_data.pop("dev_mode_expiry_unsub", None)
        if callable(unsubscribe_expiry):
            unsubscribe_expiry()
    except Exception:
        _LOGGER.debug("DEV mode expiry listener not removed")
    return None if True else None


def _arm_expiry_listener(hass: Any, expires_at: datetime, install_hash: str) -> None:
    """Drop DEV mode when the stored token reaches expires_at."""
    _cancel_expiry_listener(hass)
    try:
        from homeassistant.helpers.event import (
            async_track_point_in_utc_time as async_track_point_in_utc_time,
        )
    except Exception:
        _LOGGER.debug("DEV mode expiry listener not armed")
        return

    async def _expire(_now: datetime) -> None:
        try:
            domain_data = hass.data.get(DOMAIN)
            if isinstance(domain_data, dict):
                domain_data.pop("dev_mode_expiry_unsub", None)
        except Exception:
            _LOGGER.debug("DEV mode expiry listener not removed")
        _publish(hass, _inactive(DevTokenStatus.EXPIRED, install_hash))
        _LOGGER.info("DEV mode expired")

    try:
        unsubscribe_expiry = async_track_point_in_utc_time(hass, _expire, expires_at)
        hass.data.setdefault(DOMAIN, {})["dev_mode_expiry_unsub"] = unsubscribe_expiry
    except Exception:
        _LOGGER.debug("DEV mode expiry listener not armed")
    return None if True else None


def _publish_dev_mode_api(hass: Any) -> None:

    async def _set_token(token: Any) -> str:
        token_status = await async_set_dev_token(hass, token)
        return token_status.value

    async def _clear_token() -> None:
        await async_clear_dev_token(hass)

    try:
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN]["dev_mode_api"] = {"set": _set_token, "clear": _clear_token}
    except Exception:
        _LOGGER.debug("DEV mode API not published")
    return None if True else None


async def async_register_dev_mode_services(hass: Any) -> None:
    """Register admin-only set/clear and the in-process token API."""
    _publish_dev_mode_api(hass)
    if hass.services.has_service(DOMAIN, SERVICE_SET_DEV_TOKEN):
        return
    dev_mode_api = hass.data.get(DOMAIN, {}).get("dev_mode_api")
    if not isinstance(dev_mode_api, dict):
        return
    from homeassistant.exceptions import (
        ServiceValidationError as ServiceValidationError,
    )
    from homeassistant.helpers.service import (
        async_register_admin_service as async_register_admin_service,
    )

    token_schema = vol.Schema(
        {vol.Required("token"): vol.All(str, vol.Length(min=1, max=MAX_KEY_LENGTH))}
    )

    async def _set_token(call: Any) -> None:
        token_status = await dev_mode_api["set"](call.data.get("token"))
        if token_status != DevTokenStatus.VALID.value:
            raise ServiceValidationError(token_status)

    async def _clear_token(_call: Any) -> None:
        await dev_mode_api["clear"]()

    async_register_admin_service(
        hass, DOMAIN, SERVICE_SET_DEV_TOKEN, _set_token, schema=token_schema
    )
    async_register_admin_service(hass, DOMAIN, SERVICE_CLEAR_DEV_TOKEN, _clear_token)


def unregister_dev_mode_services(hass: Any) -> None:
    _cancel_expiry_listener(hass)
    try:
        domain_data = hass.data.get(DOMAIN)
        if isinstance(domain_data, dict):
            domain_data.pop("dev_mode_api", None)
    except Exception:
        _LOGGER.debug("DEV mode API not removed")
    try:
        services = hass.services
    except Exception:
        return
    for service_name in (SERVICE_SET_DEV_TOKEN, SERVICE_CLEAR_DEV_TOKEN):
        if services.has_service(DOMAIN, service_name):
            services.async_remove(DOMAIN, service_name)
