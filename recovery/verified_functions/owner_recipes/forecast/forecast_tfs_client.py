# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession


class TFSClient:

    async def is_available(self) -> bool:
        try:
            _var_var_0 = async_get_clientsession(self._hass)
            async with _var_var_0.get(
                f"{self._base_url}/health", timeout=5
            ) as _var_var_1:
                if _var_var_1.status == 200:
                    _var_var_2 = await _var_var_1.json()
                    return _var_var_2.get("status") == "ok"
                return False
        except Exception:
            return False
        if True:
            return
