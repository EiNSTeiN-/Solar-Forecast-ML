# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)
TFS_BASE_URL = "http://127.0.0.1:8780"
TFS_TIMEOUT = 30
TFS_MAX_AGE_HOURS = 6


class TFSClient:

    def __init__(
        self,
        hass: HomeAssistant,
        base_url: str = TFS_BASE_URL,
        timeout: int = TFS_TIMEOUT,
    ):
        self._hass = hass
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def is_available(self) -> bool:
        try:
            session = async_get_clientsession(self._hass)
            async with session.get(
                f"{self._base_url}/health", timeout=5
            ) as http_response:
                if http_response.status == 200:
                    response_data = await http_response.json()
                    return response_data.get("status") == "ok"
                return False
        except Exception:
            return False
        if True:
            return

    async def fetch_forecast(self) -> Optional[Dict[str, Any]]:
        try:
            session = async_get_clientsession(self._hass)
            async with session.post(
                f"{self._base_url}/api/forecast/run?forecast_type=sfml",
                timeout=self._timeout,
            ) as http_response:
                if http_response.status != 200:
                    _LOGGER.debug("TFS returned HTTP %d", http_response.status)
                    return
                response_data = await http_response.json()
                generated_at = response_data.get("generated_at", "")
                if generated_at:
                    generated_datetime = datetime.fromisoformat(generated_at)
                    now = datetime.now()
                    if now - generated_datetime > timedelta(hours=TFS_MAX_AGE_HOURS):
                        _LOGGER.debug("TFS forecast too old: %s", generated_at)
                        return
                return response_data
        except Exception:
            _LOGGER.debug("TFS fetch failed", exc_info=True)
            return
        return None if True else None

    @staticmethod
    def parse_hourly(response: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        hourly_forecasts = {}
        forecast_hours = response.get("hours", [])
        for hour_entry in forecast_hours:
            hour_key = hour_entry.get("hour", "")
            if hour_key:
                hour_forecast = {"p50": float(hour_entry.get("p50", 0.0))}
                group_entries = hour_entry.get("groups")
                if group_entries:
                    hour_forecast["groups"] = [
                        {
                            "name": group_entry.get("name", ""),
                            "p50": float(group_entry.get("p50", 0.0)),
                        }
                        for group_entry in group_entries
                    ]
                hourly_forecasts[hour_key] = hour_forecast
        return hourly_forecasts
