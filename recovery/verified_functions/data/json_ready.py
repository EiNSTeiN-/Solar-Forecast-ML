"""Isolated recovered source; its containing module remains incomplete.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""

from datetime import datetime
from typing import Any, Mapping


def json_ready(value: Any) -> Any:
    """Convert model objects into values canonical JSON can encode."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        raise ValueError("artifact plaintext contains raw bytes")
    if hasattr(value, "tolist"):
        return json_ready(value.tolist())
    if hasattr(value, "item") and (not isinstance(value, (str, bytes))):
        try:
            return json_ready(value.item())
        except (AttributeError, ValueError):
            pass
    if isinstance(value, Mapping):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    raise ValueError(f"unsupported artifact value: {type(value).__name__}")
