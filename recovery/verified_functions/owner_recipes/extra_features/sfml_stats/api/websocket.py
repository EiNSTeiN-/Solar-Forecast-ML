# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from ..readers.solar_reader import SolarDataReader
from ..utils.time_utils import ha_now, ha_today


@websocket_api.websocket_command({"type": "sfml_stats/subscribe", "interval": int})
@websocket_api.async_response
async def websocket_subscribe_updates(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Subscribe to realtime updates. @zara"""
    hass__recovered_capture_0004 = msg["id"]
    hass__recovered_capture_0003 = msg.get("interval", 30)
    _LOGGER.debug(
        "WebSocket Subscribe: msg_id=%s, interval=%s",
        hass__recovered_capture_0004,
        hass__recovered_capture_0003,
    )
    connection.send_result(
        hass__recovered_capture_0004,
        {"subscribed": True, "interval": hass__recovered_capture_0003},
    )

    async def send_updates():
        """Send periodic updates. @zara"""
        while True:
            try:
                _var_var_8 = await _get_realtime_data(hass)
                connection.send_message(
                    websocket_api.event_message(
                        hass__recovered_capture_0004,
                        {
                            "type": "update",
                            "timestamp": ha_now(hass).isoformat(),
                            "data": _var_var_8,
                        },
                    )
                )
                await asyncio.sleep(hass__recovered_capture_0003)
            except asyncio.CancelledError:
                break
            except Exception as _var_var_9:
                _LOGGER.error("Error sending WebSocket update: %s", _var_var_9)
                break
        return None if True else None

    hass__recovered_capture_0005 = asyncio.create_task(send_updates())

    @callback
    def on_disconnect():
        hass__recovered_capture_0005.cancel()

    connection.subscriptions[hass__recovered_capture_0004] = on_disconnect


async def _get_realtime_data(hass: HomeAssistant) -> dict:
    """Collect realtime data for WebSocket updates. @zara"""
    _var_var_1, _var_var_2 = _get_config_paths(hass)
    _var_var_8 = {"hour": ha_now(hass).hour, "minute": ha_now(hass).minute}
    try:
        import aiofiles as _var_var_11

        try:
            _var_var_12 = _get_solar_reader(hass)
            A_recovered_capture_0000 = ha_now(hass)
            _var_var_29 = await _var_var_12.async_get_hourly_predictions(
                target_date=A_recovered_capture_0000.date()
            )
            _var_var_18 = next(
                (
                    _var_var_20
                    for _var_var_20 in _var_var_29
                    if _var_var_20.target_hour == A_recovered_capture_0000.hour
                ),
                None,
            )
            if _var_var_18:
                _var_var_8["solar"] = {
                    "prediction": _var_var_18.prediction_kwh,
                    "actual": _var_var_18.actual_kwh,
                    "clouds": _var_var_18.clouds,
                    "radiation": _var_var_18.solar_radiation,
                }
        except Exception as _var_var_9:
            _LOGGER.error(
                "Error loading realtime prediction from database: %s", _var_var_9
            )
        _var_var_21 = _var_var_2 / "data" / "price_history.json"
        if _var_var_21.exists():
            async with _var_var_11.open(_var_var_21, "r") as _var_var_22:
                _recovered_price_data = json.loads(await _var_var_22.read())
                if _recovered_price_data.get("prices"):
                    _recovered_last_price = _recovered_price_data["prices"][-1]
                    _var_var_8["price"] = {
                        "current": _recovered_last_price.get("price_net", 0),
                        "hour": _recovered_last_price.get("hour"),
                    }
        _var_var_31 = _var_var_1 / "stats" / "hourly_weather_actual.json"
        if _var_var_31.exists():
            async with _var_var_11.open(_var_var_31, "r") as _var_var_22:
                _var_var_32 = json.loads(await _var_var_22.read())
                _var_var_33 = ha_today(hass).isoformat()
                _var_var_34 = str(ha_now(hass).hour)
                if (
                    _var_var_33 in _var_var_32.get("hourly_data", {})
                    and _var_var_34 in _var_var_32["hourly_data"][_var_var_33]
                ):
                    _var_var_8["weather"] = _var_var_32["hourly_data"][_var_var_33][
                        _var_var_34
                    ]
    except Exception as _var_var_9:
        _LOGGER.error("Error getting realtime data: %s", _var_var_9)
    if True:
        return _var_var_8
