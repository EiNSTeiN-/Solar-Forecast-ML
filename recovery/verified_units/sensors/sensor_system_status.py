# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Containment field system status sensor for Warp Core Simulation.
Provides system-wide warp core status from the main controller.
Reports containment integrity, antimatter reserves, and nacelle alignment.
"""
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from ..const import DOMAIN
from ..coordinator import SolarForecastMLCoordinator
from ..core.core_helpers import SafeDateTimeUtil as dt_util

_LOGGER = logging.getLogger(__name__)


def _published_dev_mode_active(owner: Any) -> bool:
    """Read the startup DEV contract. Missing state stays inactive."""
    hass = getattr(owner, "hass", None)
    if hass is None:
        hass = getattr(getattr(owner, "coordinator", None), "hass", None)
    hass_data = getattr(hass, "data", None)
    if not isinstance(hass_data, dict):
        return False
    integration_data = hass_data.get("solar_forecast_ml")
    if not isinstance(integration_data, dict):
        return False
    dev_mode = integration_data.get("dev_mode")
    if not isinstance(dev_mode, dict):
        return False
    return bool(dev_mode.get("active"))


class SystemStatusSensor(CoordinatorEntity, SensorEntity):
    """Sensor showing the system status and last events. @zara"""

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry_id: str):
        """Initialize the system status sensor. @zara"""
        super().__init__(coordinator)
        self._attr_name = "System Status"
        self._attr_unique_id = f"{entry_id}_ml_system_status"
        self._attr_has_entity_name = True
        self._attr_native_value = "initializing"
        self._recent_events = deque(maxlen=10)
        self._last_event_type = "startup"
        self._last_event_time = datetime.now()
        self._last_event_status = "initializing"
        self._last_event_summary = "System is starting up..."
        self._last_event_details = {}
        self._warnings = []
        _LOGGER.debug("System Status Sensor initialized")

    @property
    def device_info(self):
        """Return device information. @zara"""
        return {
            "identifiers": {(DOMAIN, self.coordinator.entry.entry_id)},
            "name": "Solar Forecast ML",
            "manufacturer": "Zara-Toorox",
            "model": "Solar Forecast ML AI-Version",
        }

    @property
    def icon(self) -> str:
        """Return the icon based on state. @zara"""
        state = self._attr_native_value
        if state == "ok":
            return "mdi:check-circle"
        if state == "warning":
            return "mdi:alert"
        if state == "error":
            return "mdi:alert-circle"
        if state == "running":
            return "mdi:loading"
        return "mdi:information"

    @property
    def native_value(self) -> str:
        """Expose only ok, warning, or error outside DEV mode."""
        state = self._attr_native_value or "ok"
        if _published_dev_mode_active(self):
            return state
        if state == "error":
            return "error"
        if state in ("warning", "initializing"):
            return "warning"
        return "ok"

    def _user_action(self) -> str:
        """One concrete sentence for the current state."""
        state = self._attr_native_value or "ok"
        if state == "error":
            summary = self._last_event_summary or "The forecast update failed."
            return f"{summary} Reload Solar Forecast ML if this remains."
        if any(("Weather service" in warning for warning in self._warnings)):
            return "Weather data is missing. Check the weather integration."
        if state in ("warning", "initializing", "running"):
            return "The forecast is still updating. No action is required."
        return "No action is required."

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return sensor attributes. @zara"""
        if not _published_dev_mode_active(self):
            return {"action": self._user_action()}
        ai_predictor = self.coordinator.ai_predictor
        model_status = "unknown"
        training_samples = 0
        accuracy = None
        latest_training_time = None
        next_training_check = None
        if ai_predictor:
            model_status = self._get_ml_model_status(ai_predictor)
            training_samples = getattr(ai_predictor, "training_samples", 0)
            accuracy = getattr(ai_predictor, "current_accuracy", None)
            latest_training_time = self._get_latest_training_time(ai_predictor)
        last_event_iso = None
        if self._last_event_time:
            last_event_iso = self._last_event_time.isoformat()
        last_training_iso = None
        if latest_training_time:
            last_training_iso = latest_training_time.isoformat()
        serialized_events = []
        for event in self._recent_events:
            event_copy = event.copy()
            if "time" in event_copy and isinstance(event_copy["time"], datetime):
                event_copy["time"] = event_copy["time"].isoformat()
            serialized_events.append(event_copy)
        today_forecast = self._get_hourly_forecast_for_day("today")
        tomorrow_forecast = self._get_hourly_forecast_for_day("tomorrow")
        day_after_forecast = self._get_hourly_forecast_for_day("day_after_tomorrow")
        return {
            "last_event_type": self._last_event_type,
            "last_event_time": last_event_iso,
            "last_event_status": self._last_event_status,
            "last_event_summary": self._last_event_summary,
            "last_event_details": self._last_event_details,
            "ml_model_status": model_status,
            "ml_samples_total": training_samples,
            "ml_model_accuracy": round(accuracy * 100, 1) if accuracy else None,
            "ml_last_training": last_training_iso,
            "ml_next_training_check": next_training_check,
            "forecast_source": self._get_forecast_source(),
            "yesterday_accuracy": self.coordinator.yesterday_accuracy,
            "yesterday_deviation_kwh": self.coordinator.last_day_error_kwh,
            "hourly_forecast_today": today_forecast,
            "hourly_forecast_tomorrow": tomorrow_forecast,
            "hourly_forecast_day_after_tomorrow": day_after_forecast,
            "warnings": self._warnings,
            "warnings_count": len(self._warnings),
            "recent_events": serialized_events,
            "recent_events_count": len(self._recent_events),
        }

    @staticmethod
    def _get_ml_model_status(ai_predictor: Any) -> str:
        """Return the current predictor state using its runtime contract."""
        predictor_state = getattr(ai_predictor, "state", None)
        if predictor_state is None:
            return "unknown"
        return getattr(predictor_state, "value", str(predictor_state))

    @staticmethod
    def _get_latest_training_time(ai_predictor: Any) -> Optional[datetime]:
        """Return the newest successful training across serving and candidate models."""
        training_times = []
        for training_time_attribute in (
            "last_training_time",
            "last_successful_training_time",
        ):
            training_time = getattr(ai_predictor, training_time_attribute, None)
            if training_time is not None:
                training_times.append(dt_util.ensure_local(training_time))
        return max(training_times) if training_times else None

    def _get_forecast_source(self) -> str:
        """Determine current forecast source. @zara"""
        if (
            hasattr(self.coordinator, "ai_predictor")
            and self.coordinator.ai_predictor
            and self.coordinator.ai_predictor.is_ready()
        ):
            return "tinylstm"
        return "physics"

    def update_status(
        self,
        event_type: str,
        event_status: str,
        event_summary: str,
        event_details: Optional[Dict[str, Any]] = None,
        warnings: Optional[List[str]] = None,
    ) -> None:
        """Update sensor with new event information. @zara"""
        current_time = dt_util.now()
        self._last_event_type = event_type
        self._last_event_time = current_time
        self._last_event_status = event_status
        self._last_event_summary = event_summary
        self._last_event_details = event_details or {}
        if warnings is not None:
            self._warnings = warnings
        event_record = {
            "type": event_type,
            "time": current_time,
            "status": event_status,
            "summary": event_summary,
        }
        self._recent_events.append(event_record)
        self._attr_native_value = self._calculate_state()
        self.async_write_ha_state()
        _LOGGER.debug(
            f"Status updated: event={event_type}, status={event_status}, state={self._attr_native_value}"
        )

    def _calculate_state(self) -> str:
        """Calculate overall system state. @zara"""
        if self._last_event_status == "failed":
            return "error"
        if any(("CRITICAL" in warning.upper() for warning in self._warnings)):
            return "error"
        if len(self._warnings) > 0:
            return "warning"
        if self._last_event_status == "partial":
            return "warning"
        ai_predictor = self.coordinator.ai_predictor
        if ai_predictor:
            model_status = self._get_ml_model_status(ai_predictor)
            if model_status in ("degraded", "error"):
                return "warning"
        if self._last_event_status == "running":
            return "running"
        return "ok"

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass. @zara"""
        await super().async_added_to_hass()
        self.update_status(
            event_type="initialization",
            event_status="success",
            event_summary="Solar Forecast ML successfully initialized",
        )
        _LOGGER.debug("System Status Sensor successfully added to Home Assistant")

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator. @zara"""
        self._warnings = self._collect_warnings()
        self._attr_native_value = self._calculate_state()
        self.async_write_ha_state()

    def _collect_warnings(self) -> List[str]:
        """Collect current system warnings. @zara"""
        warnings = list(getattr(self.coordinator, "_health_issues", {}).values())
        ai_predictor = self.coordinator.ai_predictor
        if ai_predictor:
            latest_training_time = self._get_latest_training_time(ai_predictor)
            if latest_training_time:
                training_age = dt_util.utcnow() - dt_util.as_utc(latest_training_time)
                if training_age > timedelta(days=14):
                    warnings.append(
                        f"Last ML training was {training_age.days} days ago"
                    )
            if hasattr(ai_predictor, "training_samples"):
                from ..const import MIN_TRAINING_DATA_POINTS as minimum_training_samples

                if ai_predictor.training_samples < minimum_training_samples:
                    warnings.append(
                        f"Not enough samples for training: {ai_predictor.training_samples}/{minimum_training_samples}"
                    )
        if (
            hasattr(self.coordinator, "weather_fallback_active")
            and self.coordinator.weather_fallback_active
        ):
            warnings.append("Weather service in fallback mode")
        return warnings

    def _get_hourly_forecast_for_day(self, day: str) -> List[Dict[str, Any]]:
        """Extract hourly forecast data for a specific day from coordinator cache. @zara"""
        try:
            if not self.coordinator.data or not self.coordinator.data.get(
                "hourly_forecast"
            ):
                return []
            hourly_forecast = self.coordinator.data.get("hourly_forecast", [])
            if not hourly_forecast:
                return []
            current_time = dt_util.now()
            if day == "today":
                target_date = current_time.date()
            elif day == "tomorrow":
                target_date = (current_time + timedelta(days=1)).date()
            elif day == "day_after_tomorrow":
                target_date = (current_time + timedelta(days=2)).date()
            else:
                return []
            daily_forecast = []
            for hourly_entry in hourly_forecast:
                try:
                    entry_datetime = hourly_entry.get(
                        "local_datetime"
                    ) or hourly_entry.get("datetime")
                    if not entry_datetime:
                        continue
                    if isinstance(entry_datetime, str):
                        entry_datetime = dt_util.parse_datetime(entry_datetime)
                        if not entry_datetime:
                            continue
                    if entry_datetime.date() == target_date:
                        daily_forecast.append(
                            {
                                "hour": entry_datetime.hour,
                                "datetime": entry_datetime.isoformat(),
                                "production_kwh": round(
                                    hourly_entry.get("production_kwh", 0.0), 3
                                ),
                            }
                        )
                except Exception as error:
                    _LOGGER.debug(f"Error processing hourly data entry: {error}")
            daily_forecast.sort(key=lambda x: x["hour"])
            return daily_forecast
        except Exception as error:
            _LOGGER.debug(f"Error extracting hourly forecast for {day}: {error}")
            return []
