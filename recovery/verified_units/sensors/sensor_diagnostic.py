# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
Warp core diagnostic monitoring array for containment field analysis.
All sensors read from controller cache or telemetry DB - no direct file I/O.
Monitors antimatter injection stability, cochrane field harmonics, and
dilithium crystal degradation metrics.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.entity import EntityCategory
from ..const import DAILY_UPDATE_HOUR, UPDATE_INTERVAL
from ..coordinator import SolarForecastMLCoordinator
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..ai import ModelState, format_time_ago
from .sensor_base import BaseSolarSensor

_LOGGER = logging.getLogger(__name__)
ML_STATE_TRANSLATIONS = {
    ModelState.UNINITIALIZED.value: "Not yet trained",
    ModelState.TRAINING.value: "Training in progress",
    ModelState.READY.value: "Ready",
    ModelState.DEGRADED.value: "Degraded",
    ModelState.ERROR.value: "Error",
    "unavailable": "Unavailable",
    "unknown": "Unknown",
}


def _ai_snapshot(coordinator: SolarForecastMLCoordinator) -> Dict[str, Any]:
    """Return the coordinator-owned AI governance snapshot."""
    return getattr(coordinator, "ai_diagnostic_snapshot", {}) or {}


def _primary_ai_model(snapshot: Dict[str, Any]) -> tuple[str, Dict[str, Any] | None]:
    """Return serving metrics, or the selected pending candidate when none serves."""
    serving_model = snapshot.get("serving")
    if serving_model:
        return ("serving", serving_model)
    pending_model = snapshot.get("pending")
    if pending_model:
        return ("candidate_pending", pending_model)
    return ("unavailable", None)


def _latest_ai_model(snapshot: Dict[str, Any]) -> tuple[str, Dict[str, Any] | None]:
    """Return the latest trained model record without changing serving semantics."""
    pending_model = snapshot.get("pending")
    if pending_model:
        return ("candidate_pending", pending_model)
    serving_model = snapshot.get("serving")
    if serving_model:
        return ("serving", serving_model)
    return ("unavailable", None)


_GATE_STATUS_TEMPLATES = {
    "de": {
        "serving": "KI aktiv",
        "candidate_evaluating": "Kandidat in Bewertung (Tag {eligible_days} von {required_days})",
        "waiting_regime": "Wartet auf Wetterlagen-Abdeckung",
        "physics_baseline": "KI noch nicht aktiv – Physik-Baseline",
    },
    "en": {
        "serving": "AI active",
        "candidate_evaluating": "Candidate in evaluation (day {eligible_days} of {required_days})",
        "waiting_regime": "Waiting for weather-regime coverage",
        "physics_baseline": "AI not yet active – physics baseline",
    },
    "fr": {
        "serving": "IA active",
        "candidate_evaluating": "Candidat en evaluation (jour {eligible_days} sur {required_days})",
        "waiting_regime": "En attente de couverture des regimes meteo",
        "physics_baseline": "IA pas encore active – baseline physique",
    },
    "es": {
        "serving": "IA activa",
        "candidate_evaluating": "Candidato en evaluacion (dia {eligible_days} de {required_days})",
        "waiting_regime": "Esperando cobertura de regimenes meteorologicos",
        "physics_baseline": "IA aun no activa – linea base fisica",
    },
    "ru": {
        "serving": "ИИ активен",
        "candidate_evaluating": "Кандидат на оценке (день {eligible_days} из {required_days})",
        "waiting_regime": "Ожидание покрытия погодных режимов",
        "physics_baseline": "ИИ ещё не активен – физический базис",
    },
}


def _gate_ui_language(coordinator: SolarForecastMLCoordinator) -> str:
    hass = getattr(coordinator, "hass", None)
    config = getattr(hass, "config", None)
    language = getattr(config, "language", None)
    if isinstance(language, str):
        language_code = language.split("-", 1)[0].lower()
        if language_code in _GATE_STATUS_TEMPLATES:
            return language_code
    return "de"


def _gate_status_text(
    kind: str,
    coordinator: SolarForecastMLCoordinator,
    *,
    eligible_days: Any = None,
    required_days: Any = None,
) -> str:
    messages = _GATE_STATUS_TEMPLATES[_gate_ui_language(coordinator)]
    return (
        (
            messages[kind].format(
                eligible_days=eligible_days if eligible_days is not None else 0,
                required_days=required_days if required_days is not None else 7,
            )
            if True
            else None
        )
        if True
        else None
    )


def _published_dev_mode_active(owner: Any) -> bool:
    """Read the startup DEV contract. Missing state stays inactive."""
    hass = getattr(owner, "hass", None)
    if hass is None:
        hass = getattr(getattr(owner, "coordinator", None), "hass", None)
    hass_data = getattr(hass, "data", None)
    if not isinstance(hass_data, dict):
        return False
    domain_data = hass_data.get("solar_forecast_ml")
    if not isinstance(domain_data, dict):
        return False
    state = domain_data.get("dev_mode")
    if not isinstance(state, dict):
        return False
    return bool(state.get("active"))


def _diagnostic_datetime(value: Any) -> Optional[datetime]:
    """Normalize a persisted diagnostic timestamp for Home Assistant."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return
    if not isinstance(value, datetime):
        return
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.get_default_time_zone())
    return value


class YesterdayDeviationSensor(BaseSolarSensor):
    """Sensor showing the absolute forecast deviation error. @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = None
    _attr_icon = "mdi:delta"

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_yesterday_deviation"
        self._attr_translation_key = "yesterday_deviation"
        self._attr_name = "Yesterday Deviation"

    @property
    def native_value(self) -> Optional[float]:
        """Get value from coordinator. @zara"""
        deviation_kwh = getattr(self.coordinator, "last_day_error_kwh", None)
        return max(0.0, deviation_kwh) if deviation_kwh is not None else None


class EodDurationSensor(BaseSolarSensor):
    """Sensor showing the duration of the last end-of-day workflow. @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = None
    _attr_device_class = None
    _attr_state_class = None
    _attr_icon = "mdi:clock-end"

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_eod_duration"
        self._attr_translation_key = "eod_duration"
        self._attr_name = "End-of-Day Duration"

    @property
    def native_value(self) -> Optional[str]:
        """Get last EOD duration as MM:SS from coordinator. @zara"""
        duration_seconds = getattr(self.coordinator, "eod_duration_seconds", None)
        if duration_seconds is None:
            return
        minutes, seconds = divmod(int(duration_seconds), 60)
        return f"{minutes}:{seconds:02} Min"


class WorkflowStatusSensor(BaseSolarSensor):
    """Sensor showing the currently active SFML workflow."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = None
    _attr_device_class = None
    _attr_state_class = None
    _attr_icon = "mdi:progress-clock"

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_workflow_status"
        self._attr_translation_key = "workflow_status"
        self._attr_name = "Workflow Status"

    @property
    def native_value(self) -> str:
        state = getattr(self.coordinator, "active_workflow", None) or {}
        if state.get("is_running"):
            return str(state.get("workflow") or "running")
        return "idle"

    @property
    def icon(self) -> str:
        state = getattr(self.coordinator, "active_workflow", None) or {}
        if state.get("is_running"):
            return "mdi:progress-clock"
        if state.get("status") in {"error", "partial"}:
            return "mdi:alert-circle-outline"
        return "mdi:check-circle-outline"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        state = dict(getattr(self.coordinator, "active_workflow", None) or {})
        started_at = state.get("started_at")
        duration_seconds = state.get("duration_seconds")
        if state.get("is_running") and started_at:
            try:
                start_datetime = datetime.fromisoformat(started_at)
                duration_seconds = round(
                    (dt_util.now() - start_datetime).total_seconds(), 1
                )
            except (TypeError, ValueError):
                pass
        steps_completed = state.get("steps_completed")
        total_steps = state.get("total_steps")
        progress = None
        if total_steps is not None:
            progress = f"{int(steps_completed or 0)}/{int(total_steps)}"
        return (
            (
                (
                    {
                        "is_running": bool(state.get("is_running")),
                        "workflow": state.get("workflow") or "idle",
                        "status": state.get("status") or "idle",
                        "current_step": state.get("current_step"),
                        "progress": progress,
                        "steps_completed": steps_completed,
                        "total_steps": total_steps,
                        "started_at": started_at,
                        "completed_at": state.get("completed_at"),
                        "duration_seconds": duration_seconds,
                        "last_error_code": (
                            "workflow_error" if state.get("last_error") else None
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )


class CloudinessTrend1hSensor(BaseSolarSensor):
    """Sensor showing cloudiness change in the last 1 hour. @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = None
    _attr_state_class = None
    _attr_icon = "mdi:weather-partly-cloudy"

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_cloudiness_trend_1h"
        self._attr_translation_key = "cloudiness_trend_1h"

    @property
    def native_value(self) -> Optional[str]:
        """Get text interpretation from coordinator cache. @zara"""
        try:
            value = self.coordinator.cloudiness_trend_1h
            if value > 10:
                return "getting_cloudier"
            if value > 5:
                return "slightly_cloudier"
            if value < -10:
                return "getting_clearer"
            if value < -5:
                return "slightly_clearer"
        except Exception as error:
            _LOGGER.debug(f"Failed to get cloudiness_trend_1h: {error}")
            return
        return "stable"

    @property
    def icon(self) -> str:
        """Dynamic icon based on trend. @zara"""
        try:
            value = self.coordinator.cloudiness_trend_1h
            if value > 10:
                return "mdi:weather-cloudy-arrow-right"
            if value > 5:
                return "mdi:weather-partly-cloudy"
            if value < -10:
                return "mdi:weather-sunny-alert"
            if value < -5:
                return "mdi:weather-sunny"
        except Exception:
            return "mdi:weather-partly-cloudy"
        return "mdi:minus-circle-outline"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Provide numeric details. @zara"""
        try:
            value = self.coordinator.cloudiness_trend_1h
            return {
                "change_percent": round(value, 1),
                "description": "Cloud change in last hour (positive = more clouds)",
            }
        except Exception:
            return {"status": "unavailable"}


class CloudinessTrend3hSensor(BaseSolarSensor):
    """Sensor showing cloudiness change in the last 3 hours. @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = None
    _attr_state_class = None
    _attr_icon = "mdi:weather-partly-cloudy"

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_cloudiness_trend_3h"
        self._attr_translation_key = "cloudiness_trend_3h"

    @property
    def native_value(self) -> Optional[str]:
        """Get text interpretation from coordinator cache. @zara"""
        try:
            value = self.coordinator.cloudiness_trend_3h
            if value > 20:
                return "much_cloudier"
            if value > 10:
                return "getting_cloudier"
            if value < -20:
                return "much_clearer"
            if value < -10:
                return "getting_clearer"
        except Exception as error:
            _LOGGER.debug(f"Failed to get cloudiness_trend_3h: {error}")
            return
        return "relatively_stable"

    @property
    def icon(self) -> str:
        """Dynamic icon based on trend. @zara"""
        try:
            value = self.coordinator.cloudiness_trend_3h
            if value > 20:
                return "mdi:weather-pouring"
            if value > 10:
                return "mdi:weather-cloudy-arrow-right"
            if value < -20:
                return "mdi:weather-sunny-alert"
            if value < -10:
                return "mdi:weather-sunny"
        except Exception:
            return "mdi:weather-partly-cloudy"
        return "mdi:minus-circle-outline"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Provide numeric details. @zara"""
        try:
            value = self.coordinator.cloudiness_trend_3h
            return {
                "change_percent": round(value, 1),
                "description": "Cloud change in last 3 hours",
            }
        except Exception:
            return {"status": "unavailable"}


class CloudinessVolatilitySensor(BaseSolarSensor):
    """Sensor showing weather stability index (inverted volatility). @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:waves"

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_cloudiness_volatility"
        self._attr_translation_key = "cloudiness_volatility"

    @property
    def native_value(self) -> Optional[float]:
        """Get stability index from coordinator cache (inverted volatility). @zara"""
        try:
            volatility = self.coordinator.cloudiness_volatility
            stability_index = max(0.0, min(100.0, 100.0 - volatility))
            return round(stability_index, 1)
        except Exception as error:
            _LOGGER.debug(f"Failed to get cloudiness_volatility: {error}")
            return

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Provide additional context. @zara"""
        value = self.native_value
        if value is None:
            return {"status": "unavailable"}
        if value > 95:
            interpretation = "very_stable"
        elif value > 85:
            interpretation = "stable"
        elif value > 70:
            interpretation = "moderate"
        elif value > 60:
            interpretation = "variable"
        else:
            interpretation = "very_variable"
        raw_volatility = 100.0 - value
        return {
            "interpretation": interpretation,
            "stability_index": round(value, 1),
            "raw_volatility": round(raw_volatility, 1),
        }


class NextProductionStartSensor(BaseSolarSensor):
    """Sensor showing when next solar production starts. @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = None
    _attr_state_class = None
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:weather-sunset-up"

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_next_production_start"
        self._attr_translation_key = "next_production_start"
        self._attr_name = "Next Production Start"

    @property
    def native_value(self) -> Optional[datetime]:
        """Return next production start time from astronomy cache. @zara"""
        try:
            from ..astronomy.astronomy_cache_manager import (
                get_cache_manager as get_cache_manager,
            )

            now = dt_util.now()
            today = now.date()
            cache_manager = get_cache_manager(self.coordinator.data_manager._db_manager)
            if not cache_manager.is_loaded():
                _LOGGER.debug(
                    "Astronomy cache not loaded - cannot calculate next production start"
                )
                return
            today_data = cache_manager.get_day_data(today)
            if today_data:
                window_start = today_data.get("production_window_start")
                if window_start:
                    production_start = self._parse_datetime_aware(
                        window_start, now.tzinfo
                    )
                    if production_start and production_start > now:
                        return production_start
            tomorrow = today + timedelta(days=1)
            tomorrow_data = cache_manager.get_day_data(tomorrow)
            if tomorrow_data:
                window_start = tomorrow_data.get("production_window_start")
                if window_start:
                    production_start = self._parse_datetime_aware(
                        window_start, now.tzinfo
                    )
                    if production_start:
                        return production_start
        except Exception as error:
            _LOGGER.debug(f"Failed to calculate next production start: {error}")
            return
        return None

    def _parse_datetime_aware(self, dt_string: str, default_tz) -> Optional[datetime]:
        """Parse datetime string ensuring timezone awareness. @zara"""
        try:
            if not dt_string:
                return
            parsed_datetime = datetime.fromisoformat(dt_string)
            if parsed_datetime.tzinfo is None:
                parsed_datetime = parsed_datetime.replace(tzinfo=default_tz)
            return parsed_datetime
        except (ValueError, TypeError) as error:
            _LOGGER.debug(f"Could not parse datetime '{dt_string}': {error}")
            return

    @property
    def icon(self) -> str:
        """Dynamic icon based on time until production. @zara"""
        try:
            production_start = self.native_value
            if not production_start:
                return "mdi:weather-sunset-up"
            now = dt_util.now()
            time_until_start = production_start - now
            if time_until_start.total_seconds() < 3600:
                return "mdi:weather-sunny-alert"
            if time_until_start.total_seconds() < 7200:
                return "mdi:weather-sunset-up"
        except Exception:
            return "mdi:weather-sunset-up"
        return "mdi:sleep"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Provide additional context from astronomy cache. @zara"""
        try:
            from ..astronomy.astronomy_cache_manager import (
                get_cache_manager as get_cache_manager,
            )

            production_start = self.native_value
            if not production_start:
                return {"status": "unavailable"}
            now = dt_util.now()
            time_until_start = production_start - now
            production_end = None
            duration_seconds = None
            cache_manager = get_cache_manager(self.coordinator.data_manager._db_manager)
            if cache_manager.is_loaded():
                production_date = production_start.date()
                today_data = cache_manager.get_day_data(production_date)
                if today_data:
                    window_end = today_data.get("production_window_end")
                    if window_end:
                        production_end = self._parse_datetime_aware(
                            window_end, now.tzinfo
                        )
                        if production_end and production_start:
                            production_duration = production_end - production_start
                            hours = int(production_duration.total_seconds() // 3600)
                            minutes = int(
                                production_duration.total_seconds() % 3600 // 60
                            )
                            duration_seconds = f"{hours}h {minutes}m"
            seconds_remaining = int(time_until_start.total_seconds())
            if seconds_remaining < 0:
                production_status = "Production active"
            else:
                hours = seconds_remaining // 3600
                minutes = seconds_remaining % 3600 // 60
                production_status = f"{hours}h {minutes}m"
            if production_start.date() == now.date():
                date_label = "Today"
            elif production_start.date() == (now + timedelta(days=1)).date():
                date_label = "Tomorrow"
            else:
                date_label = production_start.strftime("%Y-%m-%d")
            return (
                (
                    {
                        "start_time": production_start.strftime("%H:%M"),
                        "end_time": (
                            production_end.strftime("%H:%M")
                            if production_end
                            else "Unknown"
                        ),
                        "duration": duration_seconds if duration_seconds else "Unknown",
                        "starts_in": production_status,
                        "day": date_label,
                        "production_window": (
                            f"{production_start.strftime('%H:%M')}-{production_end.strftime('%H:%M')}"
                            if production_end
                            else "Unknown"
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
        except Exception as error:
            _LOGGER.error(f"Failed to get extra attributes: {error}", exc_info=True)
            return {"status": "error"}


class LastCoordinatorUpdateSensor(BaseSolarSensor):
    """Sensor showing the timestamp of the last successful coordinator update. @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_last_coordinator_update"
        self._attr_translation_key = "last_update_timestamp"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:clock-check-outline"
        self._attr_name = "Last Update"

    @property
    def native_value(self) -> Optional[datetime]:
        """Return the timestamp. @zara"""
        return getattr(self.coordinator, "last_update_success_time", None)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Provide additional context. @zara"""
        last_success = getattr(self.coordinator, "last_update_success_time", None)
        last_attempt = getattr(self.coordinator, "last_update", None)
        return (
            (
                (
                    {
                        "last_update_iso": (
                            last_success.isoformat()
                            if isinstance(last_success, datetime)
                            else last_success if isinstance(last_success, str) else None
                        ),
                        "time_ago": (
                            format_time_ago(last_success)
                            if isinstance(last_success, datetime)
                            else "Never"
                        ),
                        "last_attempt_iso": (
                            last_attempt.isoformat()
                            if isinstance(last_attempt, datetime)
                            else last_attempt if isinstance(last_attempt, str) else None
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )


class LastMLTrainingSensor(BaseSolarSensor):
    """Sensor showing the timestamp of the last ML model training. @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_last_ai_training"
        self._attr_translation_key = "last_ai_training"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:school-outline"
        self._attr_name = "Last AI Training"

    @property
    def native_value(self) -> Optional[datetime]:
        """Return the timestamp. @zara"""
        model_role, model = _latest_ai_model(_ai_snapshot(self.coordinator))
        return _diagnostic_datetime(model.get("trained_at")) if model else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Provide additional context. @zara"""
        model_role, model = _latest_ai_model(_ai_snapshot(self.coordinator))
        training_time = self.native_value
        return (
            (
                (
                    {
                        "status": model_role,
                        "model_type": model.get("model_type") if model else None,
                        "candidate_id": model.get("candidate_id") if model else None,
                        "last_training_iso": (
                            training_time.isoformat() if training_time else None
                        ),
                        "time_ago": (
                            format_time_ago(training_time)
                            if isinstance(training_time, datetime)
                            else "Never"
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )


class NextScheduledUpdateSensor(BaseSolarSensor):
    """Sensor showing the time of the next scheduled update. @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_next_scheduled_update"
        self._attr_translation_key = "next_scheduled_update"
        self._attr_icon = "mdi:calendar-clock"
        self._attr_name = "Next Scheduled Update"

    @property
    def native_value(self) -> str:
        """Return the time of next scheduled task. @zara"""
        now = dt_util.now()
        schedule = [
            (0, 10, "Midnight Task"),
            (0, 20, "Astronomy Cache Refresh"),
            (0, 30, "Early Morning Forecast"),
            (3, 0, "Weekly AI Training" if now.weekday() == 6 else None),
            (DAILY_UPDATE_HOUR, 0, "Morning Forecast"),
            (DAILY_UPDATE_HOUR, 15, "Forecast Retry #1"),
            (DAILY_UPDATE_HOUR, 30, "Forecast Retry #2"),
            (DAILY_UPDATE_HOUR, 45, "Forecast Retry #3"),
            (23, 30, "End of Day"),
        ]
        schedule = [
            (scheduled_hour, scheduled_minute, scheduled_event)
            for scheduled_hour, scheduled_minute, scheduled_event in schedule
            if scheduled_event is not None
        ]
        for hour, minute, event in schedule:
            event_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if not now < event_time:
                continue
            return f"{event_time.strftime('%H:%M')} ({event})"
        next_update = (now + timedelta(days=1)).replace(
            hour=0, minute=10, second=0, microsecond=0
        )
        return f"{next_update.strftime('%H:%M')} (Midnight Task)"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Provide more details about scheduled tasks. @zara"""
        now = dt_util.now()
        schedule = [
            (0, 10, "Midnight Task"),
            (0, 20, "Astronomy Cache Refresh"),
            (0, 30, "Early Morning Forecast"),
            (3, 0, "Weekly AI Training" if now.weekday() == 6 else None),
            (DAILY_UPDATE_HOUR, 0, "Morning Forecast"),
            (DAILY_UPDATE_HOUR, 15, "Forecast Retry #1"),
            (DAILY_UPDATE_HOUR, 30, "Forecast Retry #2"),
            (DAILY_UPDATE_HOUR, 45, "Forecast Retry #3"),
            (23, 30, "End of Day"),
        ]
        schedule = [
            (scheduled_hour, scheduled_minute, scheduled_event)
            for scheduled_hour, scheduled_minute, scheduled_event in schedule
            if scheduled_event is not None
        ]
        next_update = None
        next_event = None
        for hour, minute, event in schedule:
            event_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if not now < event_time:
                continue
            next_update = event_time
            next_event = event
            break
        if next_update is None:
            next_update = (now + timedelta(days=1)).replace(
                hour=0, minute=10, second=0, microsecond=0
            )
            next_event = "Midnight Task"
        return {
            "next_update_time_iso": next_update.isoformat(),
            "event_type": next_event,
            "is_sunday": now.weekday() == 6,
            "morning_forecast_time": f"{DAILY_UPDATE_HOUR}:00",
            "end_of_day_time": "23:30",
        }


class MLMetricsSensor(BaseSolarSensor):
    """Sensor providing key metrics about the AI prediction model. @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_ml_metrics"
        self._attr_translation_key = "ml_metrics"
        self._attr_icon = "mdi:chart-box-outline"
        self._attr_name = "AI Status"

    @property
    def native_value(self) -> str:
        """Return AI model status with R2 score. @zara"""
        model_role, model = _primary_ai_model(_ai_snapshot(self.coordinator))
        if not _published_dev_mode_active(self):
            if model_role == "serving":
                return "forecast_active"
            return "forecast_learning"
        if not model:
            return "AI Unavailable"
        r2_score = model.get("r2_score")
        if model_role == "candidate_pending":
            return (
                f"AI Candidate Pending | R2: {r2_score:.2f}"
                if r2_score is not None
                else "AI Candidate Pending"
            )
        if r2_score is not None:
            if r2_score >= 0.8:
                quality = "Excellent"
            elif r2_score >= 0.6:
                quality = "Good"
            elif r2_score >= 0.4:
                quality = "Fair"
            else:
                quality = "Learning"
            return f"AI {quality} | R2: {r2_score:.2f}"
        return "AI Ready | R2: N/A"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Provide detailed AI model metrics. @zara"""
        if not _published_dev_mode_active(self):
            model_role, model_for_status = _primary_ai_model(
                _ai_snapshot(self.coordinator)
            )
            status = (
                "forecast_active" if model_role == "serving" else "forecast_learning"
            )
            return {"status": status}
        snapshot = _ai_snapshot(self.coordinator)
        model_role, model = _primary_ai_model(snapshot)
        serving_model = snapshot.get("serving")
        pending_model = snapshot.get("pending")
        bootstrap = snapshot.get("bootstrap") or {}
        attributes = {
            "status": model_role,
            "source": snapshot.get("source", "unavailable"),
            "model_role": model_role,
            "candidate_id": model.get("candidate_id") if model else None,
            "r2_score": model.get("r2_score") if model else None,
            "rmse": model.get("rmse") if model else None,
            "training_data_points": model.get("training_samples") if model else None,
            "feature_count": model.get("input_size") if model else None,
            "last_training": model.get("trained_at") if model else None,
            "model_type": model.get("model_type") if model else None,
            "serving_source": (
                serving_model.get("serving_source") if serving_model else None
            ),
            "pending_candidate_count": snapshot.get("pending_candidate_count", 0),
            "validation_status": "in_progress" if pending_model else "none",
            "bootstrap_active": bool(bootstrap.get("active")),
            "next_eligible_shadow_cycle": bootstrap.get("next_eligible_shadow_cycle"),
            "eligible_independent_days": bootstrap.get("eligible_independent_days"),
            "required_independent_days": bootstrap.get("required_independent_days"),
            "post_midnight_runs": bootstrap.get("post_midnight_runs"),
            "pre_sunrise_runs": bootstrap.get("pre_sunrise_runs"),
            "waiting_message": bootstrap.get("waiting_message"),
        }
        if serving_model and pending_model:
            attributes.update(
                {
                    "validation_candidate_id": pending_model.get("candidate_id"),
                    "pending_candidate_id": pending_model.get("candidate_id"),
                    "pending_r2_score": pending_model.get("r2_score"),
                    "pending_rmse": pending_model.get("rmse"),
                    "pending_training_data_points": pending_model.get(
                        "training_samples"
                    ),
                    "pending_feature_count": pending_model.get("input_size"),
                    "pending_last_training": pending_model.get("trained_at"),
                    "pending_model_type": pending_model.get("model_type"),
                }
            )
        return attributes


class AIRmseSensor(BaseSolarSensor):
    """Sensor showing AI model RMSE (Root Mean Squared Error) in kWh. @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = None
    _attr_icon = "mdi:target"

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_ai_rmse"
        self._attr_translation_key = "ai_rmse"
        self._attr_name = "AI RMSE"

    @property
    def native_value(self) -> Optional[float]:
        """Return RMSE value in kWh. @zara"""
        model_role, model = _primary_ai_model(_ai_snapshot(self.coordinator))
        rmse_kwh = model.get("rmse") if model else None
        if rmse_kwh is not None:
            return round(rmse_kwh, 3)

    @property
    def icon(self) -> str:
        """Dynamic icon based on RMSE quality. @zara"""
        rmse_kwh = self.native_value
        if rmse_kwh is None:
            return "mdi:target"
        if rmse_kwh < 0.5:
            return "mdi:target"
        if rmse_kwh < 1.0:
            return "mdi:target-account"
        if rmse_kwh < 2.0:
            return "mdi:target-variant"
        return "mdi:bullseye"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Provide RMSE interpretation and context. @zara"""
        model_role, model = _primary_ai_model(_ai_snapshot(self.coordinator))
        if not model:
            return {"status": "unavailable"}
        rmse_kwh = model.get("rmse")
        r2_score = model.get("r2_score")
        if rmse_kwh is None:
            return {
                "status": "not_trained",
                "description": "RMSE will be available after first training",
            }
        if rmse_kwh < 0.3:
            quality = "excellent"
            description = "Very precise predictions"
        elif rmse_kwh < 0.5:
            quality = "very_good"
            description = "Very good prediction accuracy"
        elif rmse_kwh < 1.0:
            quality = "good"
            description = "Good prediction accuracy"
        elif rmse_kwh < 1.5:
            quality = "fair"
            description = "Acceptable accuracy, model is learning"
        elif rmse_kwh < 2.5:
            quality = "moderate"
            description = "Moderate accuracy, more training data needed"
        else:
            quality = "learning"
            description = "Model needs more time to learn"
        return {
            "status": model_role,
            "model_type": model.get("model_type"),
            "candidate_id": model.get("candidate_id"),
            "rmse_kwh": round(rmse_kwh, 3),
            "quality": quality,
            "description": description,
            "r2_score": round(r2_score, 4) if r2_score is not None else None,
            "interpretation": f"Average deviation: +/-{rmse_kwh:.2f} kWh per hour",
        }


class ActivePredictionModelSensor(BaseSolarSensor):
    """Sensor showing which prediction model/strategy is currently active. @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_active_prediction_model"
        self._attr_translation_key = "active_prediction_model"
        self._attr_icon = "mdi:brain"
        self._attr_name = "Active Prediction Model"

    @property
    def native_value(self) -> str:
        """Return active model/strategy. @zara"""
        orchestrator = getattr(self.coordinator, "forecast_orchestrator", None)
        ai_predictor = self.coordinator.ai_predictor
        ai_available = ai_predictor is not None and ai_predictor.is_ready()
        physics_available = False
        if orchestrator:
            physics_strategy = getattr(orchestrator, "rule_based_strategy", None)
            physics_available = (
                physics_strategy is not None
                and getattr(physics_strategy, "is_available", lambda: False)()
            )
        if ai_available and physics_available:
            return "AI-Hybrid"
        if ai_available:
            return "AI"
        if physics_available:
            return "Physics"
        return "Automatic"

    @property
    def icon(self) -> str:
        """Dynamic icon based on active mode. @zara"""
        mode = self.native_value
        if mode == "AI-Hybrid":
            return "mdi:brain"
        if mode == "AI":
            return "mdi:robot"
        if mode == "Physics":
            return "mdi:atom"
        return "mdi:auto-fix"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Provide detailed model information. @zara"""
        orchestrator = getattr(self.coordinator, "forecast_orchestrator", None)
        ai_predictor = self.coordinator.ai_predictor
        ai_available = ai_predictor is not None and ai_predictor.is_ready()
        physics_available = False
        if orchestrator:
            physics_strategy = getattr(orchestrator, "rule_based_strategy", None)
            physics_available = (
                physics_strategy is not None
                and getattr(physics_strategy, "is_available", lambda: False)()
            )
        attributes = {
            "mode": self.native_value,
            "ai_available": ai_available,
            "physics_available": physics_available,
        }
        descriptions = {
            "AI-Hybrid": "AI predictions validated and enhanced by physics model",
            "AI": "Pure AI-based predictions",
            "Physics": "Physics-based model (AI not yet trained)",
            "Automatic": "Automatic fallback mode",
        }
        attributes["mode_description"] = descriptions.get(self.native_value, "Unknown")
        if ai_predictor:
            attributes["ai_ready"] = ai_available
            attributes["ai_training_samples"] = getattr(
                ai_predictor, "training_samples", 0
            )
            r2_score = getattr(ai_predictor, "current_accuracy", None)
            attributes["ai_r2_score"] = (
                round(r2_score, 3) if r2_score is not None else None
            )
            attributes["ai_confidence"] = (
                round(ai_predictor.get_base_ai_confidence() * 100, 1)
                if hasattr(ai_predictor, "get_base_ai_confidence")
                else 0.0
            )
        return attributes


class CoordinatorHealthSensor(BaseSolarSensor):
    """Sensor reflecting the health of the DataUpdateCoordinator. @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_coordinator_health"
        self._attr_translation_key = "coordinator_health"
        self._attr_icon = "mdi:heart-pulse"
        self._attr_name = "Coordinator Health"

    @property
    def native_value(self) -> str:
        """Return health status. @zara"""
        last_success = getattr(self.coordinator, "last_update_success_time", None)
        update_successful = getattr(self.coordinator, "last_update_success", True)
        if not update_successful and last_success is None:
            return "Failed Initializing"
        if not update_successful:
            return "Update Failed"
        if not last_success:
            return "Initializing"
        seconds_since_success = (dt_util.now() - last_success).total_seconds()
        interval_seconds = (
            self.coordinator.update_interval.total_seconds()
            if self.coordinator.update_interval
            else UPDATE_INTERVAL.total_seconds()
        )
        if seconds_since_success < interval_seconds * 1.5:
            return "Healthy"
        if seconds_since_success < interval_seconds * 3:
            return "Delayed"
        return "Stale"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Provide detailed metrics. @zara"""
        last_success = getattr(self.coordinator, "last_update_success_time", None)
        last_attempt = getattr(self.coordinator, "last_update", None)
        return (
            (
                (
                    {
                        "last_update_successful": getattr(
                            self.coordinator, "last_update_success", False
                        ),
                        "last_success_time_iso": (
                            last_success.isoformat()
                            if isinstance(last_success, datetime)
                            else last_success if isinstance(last_success, str) else None
                        ),
                        "last_attempt_time_iso": (
                            last_attempt.isoformat()
                            if isinstance(last_attempt, datetime)
                            else last_attempt if isinstance(last_attempt, str) else None
                        ),
                        "time_since_last_success": (
                            format_time_ago(last_success)
                            if isinstance(last_success, datetime)
                            else "Never"
                        ),
                        "update_interval_seconds": (
                            self.coordinator.update_interval.total_seconds()
                            if self.coordinator.update_interval
                            else None
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )


class DataFilesStatusSensor(BaseSolarSensor):
    """Sensor showing database status. @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    CACHE_TTL_SECONDS = 60

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_data_files_status"
        self._attr_translation_key = "data_files_status"
        self._attr_icon = "mdi:database"
        self._attr_name = "Database Status"
        self._cached_status = {}
        self._cache_timestamp = 0

    @property
    def native_value(self) -> str:
        """Return database status. @zara"""
        db_manager = self.db_manager
        if not db_manager:
            return "Not Connected"
        return "Connected"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return detailed database status. @zara"""
        db_manager = self.db_manager
        if not db_manager:
            return {"status": "unavailable"}
        return {
            "db_name": getattr(db_manager.db_path, "name", "database"),
            "connected": db_manager._db is not None,
        }


class PhysicsSamplesSensor(BaseSolarSensor):
    """Sensor showing AI training status. @zara"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:brain"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_physics_samples"
        self._attr_translation_key = "physics_samples"
        self._attr_name = "AI Samples"

    @property
    def native_value(self) -> int:
        """Return AI training sample count. @zara"""
        try:
            model_role, model = _primary_ai_model(_ai_snapshot(self.coordinator))
            return int(model.get("training_samples") or 0) if model else 0
        except Exception as error:
            _LOGGER.debug(f"Error getting AI samples: {error}")
            return 0

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Provide AI status information. @zara"""
        try:
            model_role, model = _primary_ai_model(_ai_snapshot(self.coordinator))
            if not model:
                return {"status": "not_initialized"}
            training_samples = int(model.get("training_samples") or 0)
            if training_samples == 0:
                status = "untrained"
            elif training_samples < 50:
                status = "early_learning"
            elif training_samples < 200:
                status = "learning"
            elif training_samples < 500:
                status = "good"
            else:
                status = "excellent"
            return {
                "status": model_role,
                "source": "governance_db",
                "training_samples": training_samples,
                "learning_status": status,
                "model_type": model.get("model_type"),
                "candidate_id": model.get("candidate_id"),
                "input_size": model.get("input_size"),
            }
        except Exception as error:
            _LOGGER.debug(f"Error getting AI attributes: {error}")
            return {"status": "error", "message": str(error)}


class AIGateStatusSensor(BaseSolarSensor):
    """Customer-visible progress through the model-candidate promotion gate."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:progress-clock"

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ai_gate_status"
        self._attr_translation_key = "ai_gate_status"
        self._attr_name = "AI Gate Status"

    @property
    def native_value(self) -> str:
        snapshot = _ai_snapshot(self.coordinator)
        if not _published_dev_mode_active(self):
            if snapshot.get("serving"):
                return "forecast_active"
            return "forecast_learning"
        serving_model = snapshot.get("serving")
        pending_model = snapshot.get("pending")
        gate = snapshot.get("gate") or {}
        if serving_model:
            return _gate_status_text("serving", self.coordinator)
        if pending_model:
            wait_reason = gate.get("wait_reason")
            if wait_reason == "INSUFFICIENT_REGIME_COVERAGE":
                return _gate_status_text("waiting_regime", self.coordinator)
            return _gate_status_text(
                "candidate_evaluating",
                self.coordinator,
                eligible_days=gate.get("eligible_days"),
                required_days=gate.get("required_days"),
            )
        return _gate_status_text("physics_baseline", self.coordinator)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        snapshot = _ai_snapshot(self.coordinator)
        if not _published_dev_mode_active(self):
            status = (
                "forecast_active" if snapshot.get("serving") else "forecast_learning"
            )
            return {"status": status}
        serving_model = snapshot.get("serving") or {}
        pending_model = snapshot.get("pending") or {}
        gate = snapshot.get("gate") or {}
        created_at = gate.get("candidate_created_at")
        calendar_day_limit = gate.get("calendar_day_limit")
        created_datetime = _diagnostic_datetime(created_at)
        decision_deadline = None
        if (
            created_datetime is not None
            and isinstance(calendar_day_limit, int)
            and (not isinstance(calendar_day_limit, bool))
        ):
            decision_deadline = created_datetime + timedelta(days=calendar_day_limit)
        candidate_age_days = None
        if created_datetime is not None:
            candidate_age_days = max(
                0, (dt_util.now().date() - created_datetime.date()).days
            )
        return (
            (
                (
                    {
                        "candidate_id": gate.get("candidate_id")
                        or pending_model.get("candidate_id"),
                        "eligible_days": gate.get("eligible_days"),
                        "required_days": gate.get("required_days"),
                        "policy_eligible_days": gate.get("policy_eligible_days"),
                        "wait_reason": gate.get("wait_reason"),
                        "serving_source": serving_model.get("serving_source"),
                        "candidate_age_days": candidate_age_days,
                        "decision_deadline": (
                            decision_deadline.isoformat()
                            if decision_deadline is not None
                            else None
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )
