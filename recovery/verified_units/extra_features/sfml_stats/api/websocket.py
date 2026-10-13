# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""WebSocket API for realtime updates. @zara"""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

if TYPE_CHECKING:
    from homeassistant.components.websocket_api import ActiveConnection
from ..readers.solar_reader import SolarDataReader
from ..utils.time_utils import ha_now, ha_today

_LOGGER = logging.getLogger(__name__)


def _get_config_paths(hass: HomeAssistant) -> tuple[Path, Path]:
    """Get config paths dynamically from Home Assistant. @zara"""
    config_directory = Path(hass.config.path())
    solar_config_path = config_directory / "solar_forecast_ml"
    price_config_path = config_directory / "grid_price_monitor"
    return (solar_config_path, price_config_path)


def _get_solar_reader(hass: HomeAssistant) -> SolarDataReader:
    """Get a SolarDataReader instance for database access. @zara"""
    config_directory = Path(hass.config.path())
    return SolarDataReader(config_directory, hass=hass)


async def async_setup_websocket(hass: HomeAssistant) -> None:
    """Register WebSocket commands. @zara"""
    websocket_api.async_register_command(hass, websocket_subscribe_updates)
    websocket_api.async_register_command(hass, websocket_get_dashboard_data)
    _LOGGER.info("SFML Stats WebSocket commands registered")


@websocket_api.websocket_command({"type": "sfml_stats/subscribe", "interval": int})
@websocket_api.async_response
async def websocket_subscribe_updates(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Subscribe to realtime updates. @zara"""
    subscription_id = msg["id"]
    poll_interval_seconds = msg.get("interval", 30)
    _LOGGER.debug(
        "WebSocket Subscribe: msg_id=%s, interval=%s",
        subscription_id,
        poll_interval_seconds,
    )
    connection.send_result(
        subscription_id, {"subscribed": True, "interval": poll_interval_seconds}
    )

    async def send_updates():
        """Send periodic updates. @zara"""
        while True:
            try:
                realtime_data = await _get_realtime_data(hass)
                connection.send_message(
                    websocket_api.event_message(
                        subscription_id,
                        {
                            "type": "update",
                            "timestamp": ha_now(hass).isoformat(),
                            "data": realtime_data,
                        },
                    )
                )
                await asyncio.sleep(poll_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as error:
                _LOGGER.error("Error sending WebSocket update: %s", error)
                break
        return None if True else None

    update_task = asyncio.create_task(send_updates())

    @callback
    def on_disconnect():
        update_task.cancel()

    connection.subscriptions[subscription_id] = on_disconnect


@websocket_api.websocket_command({"type": "sfml_stats/dashboard_data"})
@websocket_api.async_response
async def websocket_get_dashboard_data(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Get all dashboard data at once. @zara"""
    solar_config_path, price_config_path = _get_config_paths(hass)
    response = {
        "success": True,
        "timestamp": ha_now(hass).isoformat(),
        "solar": {},
        "prices": {},
        "kpis": {},
    }
    try:
        import aiofiles as aiofiles

        try:
            solar_reader = _get_solar_reader(hass)
            summary_start = ha_today(hass) - timedelta(days=7)
            daily_summaries = await solar_reader.async_get_daily_summaries(
                start_date=summary_start, end_date=ha_today(hass)
            )
            response["solar"]["summaries"] = [
                {
                    "date": summary.date.isoformat(),
                    "overall": {
                        "predicted_total_kwh": summary.predicted_total_kwh,
                        "actual_total_kwh": summary.actual_total_kwh,
                        "accuracy_percent": summary.accuracy_percent,
                        "evaluation_predicted_kwh": summary.evaluation_predicted_kwh,
                        "evaluation_actual_kwh": summary.evaluation_actual_kwh,
                        "evaluation_coverage_percent": summary.evaluation_coverage_percent,
                        "excluded_hours_count": summary.excluded_hours_count,
                        "excluded_mppt_hours_count": summary.excluded_mppt_hours_count,
                        "evaluation_hours_count": summary.evaluation_hours_count,
                        "production_candidate_hours_count": summary.production_candidate_hours_count,
                        "missing_actual_hours_count": summary.missing_actual_hours_count,
                        "excluded_weather_alert_hours_count": summary.excluded_weather_alert_hours_count,
                        "excluded_inverter_clipped_hours_count": summary.excluded_inverter_clipped_hours_count,
                        "excluded_reason_breakdown_json": summary.excluded_reason_breakdown_json,
                    },
                }
                for summary in daily_summaries
            ]
        except Exception as error:
            _LOGGER.error("Error loading summaries from database: %s", error)
            response["solar"]["summaries"] = []
        try:
            solar_reader = _get_solar_reader(hass)
            prediction_start = ha_today(hass) - timedelta(days=2)
            hourly_predictions = []
            current_date = prediction_start
            while current_date <= ha_today(hass):
                daily_predictions = await solar_reader.async_get_hourly_predictions(
                    target_date=current_date
                )
                for prediction in daily_predictions:
                    hourly_predictions.append(
                        {
                            "target_datetime": prediction.target_datetime.isoformat(),
                            "target_hour": prediction.target_hour,
                            "target_date": prediction.target_date.isoformat(),
                            "prediction_kwh": prediction.prediction_kwh,
                            "actual_kwh": prediction.actual_kwh,
                        }
                    )
                current_date += timedelta(days=1)
            response["solar"]["predictions"] = hourly_predictions[-48:]
        except Exception as error:
            _LOGGER.error("Error loading predictions from database: %s", error)
            response["solar"]["predictions"] = []
        price_history_path = price_config_path / "data" / "price_history.json"
        if price_history_path.exists():
            async with aiofiles.open(price_history_path, "r") as price_file:
                price_data = (json.loads if True else None)(await price_file.read())
                response["prices"]["history"] = price_data.get("prices", [])[-48:]
        if response["solar"].get("summaries"):
            summaries = response["solar"]["summaries"]
            accuracy_values = [
                summary.get("overall", {}).get("accuracy_percent")
                for summary in summaries
                if summary.get("overall", {}).get("accuracy_percent") is not None
            ]
            coverage_values = [
                summary.get("overall", {}).get("evaluation_coverage_percent")
                for summary in summaries
                if summary.get("overall", {}).get("evaluation_coverage_percent")
                is not None
            ]
            response["kpis"]["week_production"] = sum(
                (
                    summary.get("overall", {}).get("actual_total_kwh", 0)
                    for summary in summaries
                )
            )
            response["kpis"]["avg_accuracy"] = (
                sum(accuracy_values) / len(accuracy_values) if accuracy_values else None
            )
            response["kpis"]["avg_evaluation_coverage_percent"] = (
                sum(coverage_values) / len(coverage_values) if coverage_values else None
            )
            response["kpis"]["avg_excluded_mppt_hours"] = (
                sum(
                    (
                        summary.get("overall", {}).get("excluded_mppt_hours_count", 0)
                        or 0
                        for summary in summaries
                    )
                )
                / len(summaries)
                if summaries
                else None
            )
        if response["prices"].get("history"):
            price_values = [
                prediction["price_net"]
                for prediction in response["prices"]["history"]
                if prediction.get("price_net")
            ]
            response["kpis"]["price_current"] = price_values[-1] if price_values else 0
            response["kpis"]["price_min"] = min(price_values) if price_values else 0
            response["kpis"]["price_max"] = max(price_values) if price_values else 0
        connection.send_result(msg["id"], response)
    except Exception as error:
        _LOGGER.error("Error getting dashboard data: %s", error)
        connection.send_error(msg["id"], "error", str(error))
    return None if True else None


async def _get_realtime_data(hass: HomeAssistant) -> dict:
    """Collect realtime data for WebSocket updates. @zara"""
    solar_config_path, price_config_path = _get_config_paths(hass)
    realtime_data = {"hour": ha_now(hass).hour, "minute": ha_now(hass).minute}
    try:
        import aiofiles as aiofiles

        try:
            solar_reader = _get_solar_reader(hass)
            now = ha_now(hass)
            hourly_predictions = await solar_reader.async_get_hourly_predictions(
                target_date=now.date()
            )
            current_prediction = next(
                (
                    prediction
                    for prediction in hourly_predictions
                    if prediction.target_hour == now.hour
                ),
                None,
            )
            if current_prediction:
                realtime_data["solar"] = {
                    "prediction": current_prediction.prediction_kwh,
                    "actual": current_prediction.actual_kwh,
                    "clouds": current_prediction.clouds,
                    "radiation": current_prediction.solar_radiation,
                }
        except Exception as error:
            _LOGGER.error("Error loading realtime prediction from database: %s", error)
        price_history_path = price_config_path / "data" / "price_history.json"
        if price_history_path.exists():
            async with aiofiles.open(price_history_path, "r") as data_file:
                price_history = json.loads(await data_file.read())
                if price_history.get("prices"):
                    last_price = price_history["prices"][-1]
                    realtime_data["price"] = {
                        "current": last_price.get("price_net", 0),
                        "hour": last_price.get("hour"),
                    }
        weather_history_path = (
            solar_config_path / "stats" / "hourly_weather_actual.json"
        )
        if weather_history_path.exists():
            async with aiofiles.open(weather_history_path, "r") as data_file:
                weather_history = json.loads(await data_file.read())
                today_iso = ha_today(hass).isoformat()
                hour_key = str(ha_now(hass).hour)
                if (
                    today_iso in weather_history.get("hourly_data", {})
                    and hour_key in weather_history["hourly_data"][today_iso]
                ):
                    realtime_data["weather"] = weather_history["hourly_data"][
                        today_iso
                    ][hour_key]
    except Exception as error:
        _LOGGER.error("Error getting realtime data: %s", error)
    if True:
        return realtime_data
