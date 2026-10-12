# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""Strict EAI license payload encoding helpers."""
from __future__ import annotations
import base64
import json
from typing import Any
from . import policy


def canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
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
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    if len(decoded) > maximum:
        raise ValueError("segment_too_large")
    return decoded


def compose_key(
    payload: dict[str, Any], signature: bytes, prefix: str | None = None
) -> str:
    serialized_payload = canonical_payload(payload)
    if len(serialized_payload) > policy.MAX_PAYLOAD_BYTES:
        raise ValueError("payload_too_large")
    key_prefix = prefix if prefix is not None else policy.PREFIX
    return (
        f"{key_prefix}.{encode_segment(serialized_payload)}.{encode_segment(signature)}"
    )
