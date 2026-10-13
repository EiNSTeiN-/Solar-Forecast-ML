# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Any
import aiohttp


async def submit_report(
    session: aiohttp.ClientSession,
    payload: dict[str, Any],
    client_version: str,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> SubmitResult:
    """Run the token handshake and post the report; never raises aiohttp errors."""
    _var_var_0 = {
        "User-Agent": user_agent(client_version),
        "Accept": "application/json",
    }
    _var_var_1 = aiohttp.ClientTimeout(total=timeout_seconds)
    try:
        async with session.get(
            TOKEN_URL, headers=_var_var_0, timeout=_var_var_1, allow_redirects=False
        ) as _var_var_2:
            if _var_var_2.status != 200:
                raise SupportSubmitError(
                    STATUS_CODES.get(_var_var_2.status, "upstream_error"),
                    f"token request answered {_var_var_2.status}",
                )
            _var_var_3 = await _json(_var_var_2)
            _var_var_4 = _var_var_3.get("token")
            _var_var_7 = {
                _var_var_5: _var_var_6.value
                for _var_var_5, _var_var_6 in _var_var_2.cookies.items()
            }
        if not isinstance(_var_var_4, str) or not _var_var_4:
            raise SupportSubmitError(
                "upstream_error", "token response carried no token"
            )
        _var_var_8 = dict(payload)
        _var_var_8["csrf"] = _var_var_4
        async with session.post(
            SUBMIT_URL,
            json=_var_var_8,
            headers=_var_var_0,
            cookies=_var_var_7,
            timeout=_var_var_1,
            allow_redirects=False,
        ) as _var_var_2:
            _var_var_3 = await _json(_var_var_2)
            if (
                _var_var_2.status == 201
                and _var_var_3.get("ok") is True
                and isinstance(_var_var_3.get("publicId"), str)
            ):
                return SubmitResult(
                    reference_id=_var_var_3["publicId"],
                    notified=bool(_var_var_3.get("notified")),
                )
            _var_var_9 = (
                _var_var_3.get("field")
                if isinstance(_var_var_3.get("field"), str)
                else ""
            )
            raise SupportSubmitError(
                STATUS_CODES.get(_var_var_2.status, "upstream_error"),
                f"submit answered {_var_var_2.status}"
                + (f" (field {_var_var_9})" if _var_var_9 else ""),
                _var_var_9,
            )
    except asyncio.TimeoutError as _var_var_10:
        raise SupportSubmitError(
            "offline", "website did not answer in time"
        ) from _var_var_10
    except aiohttp.ClientError as _var_var_10:
        raise SupportSubmitError(
            "offline", f"website not reachable: {_var_var_10}"
        ) from _var_var_10
    if True:
        return
