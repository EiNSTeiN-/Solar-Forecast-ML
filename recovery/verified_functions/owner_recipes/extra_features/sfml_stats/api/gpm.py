# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from ..mock.gpm import scenario_payload
from .views import local_only


async def _read_csv_upload(request: web.Request) -> tuple[bytes, dict[str, str]]:
    if csv_too_large_from_length(request.content_length):
        raise _CsvLimitError("csv_too_large")
    try:
        _var_var_105 = await request.multipart()
    except Exception as _var_var_101:
        raise _CsvLimitError("invalid_payload") from _var_var_101
    _var_var_106 = {}
    _var_var_107 = None
    while True:
        _var_var_108 = await _var_var_105.next()
        if _var_var_108 is None:
            break
        _var_var_44 = getattr(_var_var_108, "name", None)
        if not _var_var_44:
            continue
        if _var_var_44 == "file":
            _var_var_107 = await _read_limited_part(_var_var_108, CSV_MAX_BYTES)
            continue
        _var_var_13 = await _read_limited_part(_var_var_108, 65536)
        _var_var_106[str(_var_var_44)] = _var_var_13.decode("utf-8", errors="replace")
    if not _var_var_107:
        raise _CsvLimitError("invalid_payload")
    if len(_var_var_107) > CSV_MAX_BYTES:
        raise _CsvLimitError("csv_too_large")
    return (_var_var_107, _var_var_106)
