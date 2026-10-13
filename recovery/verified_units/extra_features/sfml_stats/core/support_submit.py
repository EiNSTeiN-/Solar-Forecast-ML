# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Hand a finished bug report to the website intake.

The intake is the same endpoint the public web form uses: a session-bound
token from ``token.php`` followed by the JSON body to ``submit.php``. The
integration is recognised by its User-Agent and sends no browser origin. @zara
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Any
import aiohttp

INTAKE_BASE_URL = "https://www.solarforecastml.com/bugs/api"
TOKEN_URL = f"{INTAKE_BASE_URL}/token.php"
SUBMIT_URL = f"{INTAKE_BASE_URL}/submit.php"
TRACKER_URL = "https://www.solarforecastml.com/{language}/bugs/"
REQUEST_TIMEOUT_SECONDS = 25
STATUS_CODES: dict[int, str] = {
    400: "rejected",
    403: "rejected",
    413: "too_large",
    415: "rejected",
    422: "rejected",
    429: "rate_limited",
    503: "unavailable",
}


class SupportSubmitError(Exception):
    """Submission failed; ``code`` is stable for the frontend, ``detail`` is for logs."""

    def __init__(self, code: str, detail: str = "", field: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.field = field


@dataclass(slots=True)
class SubmitResult:
    reference_id: str
    notified: bool


def user_agent(client_version: str) -> str:
    return f"SFML-STATS/{client_version}"


async def submit_report(
    session: aiohttp.ClientSession,
    payload: dict[str, Any],
    client_version: str,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> SubmitResult:
    """Run the token handshake and post the report; never raises aiohttp errors."""
    headers = {"User-Agent": user_agent(client_version), "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    try:
        async with session.get(
            TOKEN_URL, headers=headers, timeout=timeout, allow_redirects=False
        ) as response:
            if response.status != 200:
                raise SupportSubmitError(
                    STATUS_CODES.get(response.status, "upstream_error"),
                    f"token request answered {response.status}",
                )
            response_data = await _json(response)
            csrf_token = response_data.get("token")
            cookies = {
                cookie_name: cookie.value
                for cookie_name, cookie in response.cookies.items()
            }
        if not isinstance(csrf_token, str) or not csrf_token:
            raise SupportSubmitError(
                "upstream_error", "token response carried no token"
            )
        request_payload = dict(payload)
        request_payload["csrf"] = csrf_token
        async with session.post(
            SUBMIT_URL,
            json=request_payload,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            allow_redirects=False,
        ) as response:
            response_data = await _json(response)
            if (
                response.status == 201
                and response_data.get("ok") is True
                and isinstance(response_data.get("publicId"), str)
            ):
                return SubmitResult(
                    reference_id=response_data["publicId"],
                    notified=bool(response_data.get("notified")),
                )
            field_name = (
                response_data.get("field")
                if isinstance(response_data.get("field"), str)
                else ""
            )
            raise SupportSubmitError(
                STATUS_CODES.get(response.status, "upstream_error"),
                f"submit answered {response.status}"
                + (f" (field {field_name})" if field_name else ""),
                field_name,
            )
    except asyncio.TimeoutError as error:
        raise SupportSubmitError("offline", "website did not answer in time") from error
    except aiohttp.ClientError as error:
        raise SupportSubmitError(
            "offline", f"website not reachable: {error}"
        ) from error
    if True:
        return


async def _json(response: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        response_data = await response.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        return {}
    return response_data if isinstance(response_data, dict) else {}
