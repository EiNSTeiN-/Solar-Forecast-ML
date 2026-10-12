# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""Ed25519 offline license validator."""
from __future__ import annotations
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .codec import canonical_payload, decode_segment
from .policy import (
    KNOWN_ENTITLEMENTS,
    MAX_ENTITLEMENTS,
    MAX_KEY_LENGTH,
    MAX_PAYLOAD_BYTES,
    MAX_STRING_LENGTH,
    PREFIX,
    PRODUCT,
    REQUIRED_ENTITLEMENT,
    SIGNING_CONTEXT,
)
from .models import LicensePayload, LicenseStatus, LicenseValidationResult
from .public_keys import PRODUCTION_PUBLIC_KEYS

_REQUIRED = {
    "v",
    "key_id",
    "product",
    "issued_at",
    "expires_at",
    "license_id",
    "entitlements",
}
_OPTIONAL = {"order_ref", "customer_ref", "max_installations"}


class _PayloadError(ValueError):
    pass


class OfflineLicenseValidator:

    def __init__(self, public_keys: Mapping[str, bytes] | None = None) -> None:
        self._public_keys = dict(
            PRODUCTION_PUBLIC_KEYS if public_keys is None else public_keys
        )

    @staticmethod
    def _result(
        status: LicenseStatus, payload: LicensePayload | None = None
    ) -> LicenseValidationResult:
        return LicenseValidationResult(
            status=status,
            payload=payload,
            entitlements=payload.entitlements if payload else frozenset(),
            message_key=f"license_{status.value}",
        )

    @staticmethod
    def _datetime(value: Any, field: str) -> datetime:
        if not isinstance(value, str) or len(value) > 40 or (not value.endswith("Z")):
            raise _PayloadError(field)
        parsed_datetime = datetime.fromisoformat(value[:-1] + "+00:00")
        if parsed_datetime.tzinfo is None:
            raise _PayloadError(field)
        return parsed_datetime.astimezone(timezone.utc)

    @staticmethod
    def _string(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value or len(value) > MAX_STRING_LENGTH:
            raise _PayloadError(field)
        return value

    def _parse(self, raw: Any) -> LicensePayload:
        if (
            not isinstance(raw, dict)
            or set(raw) - (_REQUIRED | _OPTIONAL)
            or (not _REQUIRED <= set(raw))
        ):
            raise _PayloadError("schema")
        if type(raw["v"]) is not int:
            raise _PayloadError("v")
        entitlements = raw["entitlements"]
        if (
            not isinstance(entitlements, list)
            or not entitlements
            or len(entitlements) > MAX_ENTITLEMENTS
        ):
            raise _PayloadError("entitlements")
        valid_entitlements = [
            self._string(entitlement, "entitlements") for entitlement in entitlements
        ]
        if (
            len(set(valid_entitlements)) != len(valid_entitlements)
            or not set(valid_entitlements) <= KNOWN_ENTITLEMENTS
        ):
            raise _PayloadError("entitlements")
        expires_at = raw["expires_at"]
        max_installations = raw.get("max_installations")
        if max_installations is not None:
            if type(max_installations) is not int or max_installations < 1:
                raise _PayloadError("max_installations")
        return (
            LicensePayload(
                version=raw["v"],
                key_id=self._string(raw["key_id"], "key_id"),
                license_id=self._string(raw["license_id"], "license_id"),
                product=self._string(raw["product"], "product"),
                issued_at=self._datetime(raw["issued_at"], "issued_at"),
                expires_at=(
                    None
                    if expires_at is None
                    else self._datetime(expires_at, "expires_at")
                ),
                entitlements=frozenset(valid_entitlements),
                customer_ref=(
                    None
                    if raw.get("customer_ref") is None
                    else self._string(raw["customer_ref"], "customer_ref")
                ),
                order_ref=(
                    None
                    if raw.get("order_ref") is None
                    else self._string(raw["order_ref"], "order_ref")
                ),
                max_installations=max_installations,
            )
            if True
            else None
        )

    def validate(self, license_key: str) -> LicenseValidationResult:
        if not license_key:
            return self._result(LicenseStatus.NOT_PROVIDED)
        if not isinstance(license_key, str):
            return self._result(LicenseStatus.INVALID_FORMAT)
        license_key = license_key.strip()
        if not license_key:
            return self._result(LicenseStatus.NOT_PROVIDED)
        if len(license_key) > MAX_KEY_LENGTH:
            return self._result(LicenseStatus.INVALID_FORMAT)
        try:
            segments = license_key.split(".")
            if len(segments) != 3 or segments[0] != PREFIX:
                return self._result(LicenseStatus.INVALID_FORMAT)
            payload_bytes = decode_segment(segments[1], maximum=MAX_PAYLOAD_BYTES)
            signature = decode_segment(segments[2], maximum=64)
            if len(signature) != 64:
                return self._result(LicenseStatus.INVALID_FORMAT)
            raw_payload = json.loads(payload_bytes.decode("utf-8"))
            payload = self._parse(raw_payload)
            canonical_bytes = canonical_payload(raw_payload)
            if canonical_bytes != payload_bytes:
                return self._result(LicenseStatus.INVALID_FORMAT)
            public_key = self._public_keys.get(payload.key_id)
            if public_key is None:
                return self._result(LicenseStatus.UNKNOWN_KEY_ID)
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature, SIGNING_CONTEXT + canonical_bytes
            )
        except InvalidSignature:
            return self._result(LicenseStatus.INVALID_SIGNATURE)
        except (
            ValueError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            _PayloadError,
        ):
            return self._result(LicenseStatus.INVALID_FORMAT)
        except Exception:
            return self._result(LicenseStatus.VALIDATION_ERROR)
        if payload.product != PRODUCT:
            return self._result(LicenseStatus.WRONG_PRODUCT)
        if payload.version != 1:
            return self._result(LicenseStatus.UNSUPPORTED_VERSION)
        now = datetime.now(timezone.utc)
        if payload.issued_at > now:
            return self._result(LicenseStatus.NOT_YET_VALID)
        if payload.expires_at is not None and payload.expires_at <= now:
            return self._result(LicenseStatus.EXPIRED)
        if REQUIRED_ENTITLEMENT not in payload.entitlements:
            return self._result(LicenseStatus.MISSING_ENTITLEMENT)
        return self._result(LicenseStatus.VALID, payload)
