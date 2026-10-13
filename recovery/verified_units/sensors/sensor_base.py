# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Subspace sensor array base classes for Warp Core Simulation.
All sensors use TelemetryManager for warp field state persistence.
Cochrane field readings cached in coordinator for real-time bridge display.
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from ..const import (
    DOMAIN,
    INTEGRATION_MODEL,
    AI_VERSION,
    SOFTWARE_VERSION,
    OPERATIONAL_REFORECAST_MODE_CUSTOM_TIME,
    OPERATIONAL_REFORECAST_MODE_STANDARD,
    OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY,
    OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY_AFTERNOON,
    DATA_KEY_FORECAST_TODAY,
    DATA_KEY_FORECAST_DAY_AFTER,
    DATA_KEY_PRODUCTION_TIME,
    DATA_KEY_PEAK_TODAY,
    DATA_KEY_EXPECTED_DAILY_PRODUCTION,
    DATA_KEY_STATISTICS,
    PROD_TIME_DURATION_SECONDS,
    PEAK_TODAY_POWER_W,
    STATS_ALL_TIME_PEAK,
    STATS_CURRENT_MONTH,
    STATS_CURRENT_WEEK,
    STATS_LAST_7_DAYS,
    STATS_LAST_30_DAYS,
    STATS_AVG_ACCURACY,
    STATS_YIELD_KWH,
    STATS_AVG_YIELD_KWH,
    STATS_CONSUMPTION_KWH,
    CACHE_HOURLY_PREDICTIONS,
    CACHE_PREDICTIONS,
    CACHE_PREDICTIONS_TOMORROW,
    CACHE_PREDICTIONS_DAY_AFTER,
    CACHE_BEST_HOUR_TODAY,
    PRED_TARGET_DATE,
    PRED_TARGET_HOUR,
    PRED_PREDICTION_KWH,
    PRED_PREDICTED_KWH,
)
from ..coordinator import SolarForecastMLCoordinator
from ..data.db_manager import DatabaseManager
from ..data.data_weather_radiation_ensemble import (
    MIN_PREDICTION_HOURS_TO_LOCK_DAY,
    hourly_forecast_date_covered,
)
from ..forecast.forecast_tfs_client import TFSClient
from .conservative_planning import (
    DEFAULT_SFML_WEIGHT,
    DEFAULT_TFS_P10_WEIGHT,
    cap_persisted_conservative_value,
    conservative_planning_value,
    p10_age_hours,
    p10_morning_recompute_allowed,
    parse_p10_timestamp,
    persisted_p10_freshness,
    remaining_conservative_value,
    remaining_today_expected_kwh,
    round_not_above,
)

_LOGGER = logging.getLogger(__name__)


def _build_hourly_attributes(predictions: list) -> dict:
    """Build hourly forecast attributes from predictions list. @zara"""
    if not predictions:
        return {}
    ordered_predictions = sorted(predictions, key=lambda p: p.get("target_hour", 0))
    hourly_values = {}
    for prediction in ordered_predictions:
        target_hour = prediction.get("target_hour")
        prediction_kwh = prediction.get("prediction_kwh", 0.0)
        if target_hour is not None and isinstance(prediction_kwh, (int, float)):
            hourly_values[f"{target_hour:02d}:00"] = round(prediction_kwh, 3)
    return {"hours": hourly_values}


async def _load_active_ops_day_forecast(
    db: Any, today_str: str
) -> Optional[dict[str, Any]]:
    """Return the active operational full-day forecast assembled from OPS hourly rows."""
    ops_hourly_rows = await db.fetchall(
        "SELECT target_hour, prediction_kwh, snapshot_type, snapshot_sequence,\n                  trigger_source, source_track, reforecast_cutoff_hour\n           FROM ops_hourly_forecasts\n           WHERE target_date = ?\n             AND is_active = TRUE\n           ORDER BY target_hour",
        (today_str,),
    )
    if not ops_hourly_rows:
        return
    total_kwh = 0.0
    hourly_values = {}
    snapshot_types = set()
    source_tracks = set()
    latest_snapshot_sequence = None
    latest_trigger_source = None
    latest_cutoff_hour = None
    for row in ops_hourly_rows:
        target_hour = int(row[0])
        prediction_kwh = float(row[1] or 0.0)
        snapshot_type = str(row[2]) if row[2] is not None else None
        snapshot_sequence = int(row[3]) if row[3] is not None else None
        trigger_source = str(row[4]) if row[4] is not None else None
        source_track = str(row[5]) if row[5] is not None else None
        reforecast_cutoff_hour = int(row[6]) if row[6] is not None else None
        hourly_values[f"{target_hour:02d}:00"] = round(prediction_kwh, 3)
        total_kwh += prediction_kwh
        if snapshot_type:
            snapshot_types.add(snapshot_type)
        if source_track:
            source_tracks.add(source_track)
        if snapshot_sequence is not None:
            if (
                latest_snapshot_sequence is None
                or snapshot_sequence > latest_snapshot_sequence
            ):
                latest_snapshot_sequence = snapshot_sequence
                latest_trigger_source = trigger_source
                latest_cutoff_hour = reforecast_cutoff_hour
    panel_group_rows = await db.fetchall(
        "SELECT target_hour, group_name, prediction_kwh\n           FROM ops_prediction_panel_groups\n           WHERE target_date = ?\n             AND is_active = TRUE\n           ORDER BY target_hour, group_name",
        (today_str,),
    )
    hourly_group_values = {}
    group_totals = {}
    for row in panel_group_rows:
        target_hour = int(row[0])
        group_name = str(row[1])
        prediction_kwh = round(float(row[2] or 0.0), 4)
        hour_label = f"{target_hour:02d}:00"
        if hour_label not in hourly_group_values:
            hourly_group_values[hour_label] = {}
        hourly_group_values[hour_label][group_name] = prediction_kwh
        group_totals[group_name] = round(
            group_totals.get(group_name, 0.0) + prediction_kwh, 4
        )
    ops_daily_row = await db.fetchone(
        "SELECT prediction_kwh, snapshot_type\n           FROM ops_daily_forecasts\n           WHERE forecast_type = 'today'\n             AND forecast_date = ?\n             AND is_active = TRUE\n           ORDER BY snapshot_sequence DESC\n           LIMIT 1",
        (today_str,),
    )
    ops_daily_kwh = None
    ops_daily_scope = None
    if ops_daily_row:
        ops_daily_kwh = round(float(ops_daily_row[0] or 0.0), 2)
        snapshot_type = str(ops_daily_row[1]) if ops_daily_row[1] is not None else ""
        ops_daily_scope = (
            "rest_of_day_total"
            if snapshot_type in ("intraday_reforecast", "weather_event_reforecast")
            else "full_day_snapshot"
        )
    morning_forecast_row = await db.fetchone(
        "SELECT prediction_kwh FROM daily_forecasts\n           WHERE forecast_type = 'today' AND forecast_date = ?\n           LIMIT 1",
        (today_str,),
    )
    morning_anchor_kwh = (
        round(float(morning_forecast_row[0] or 0.0), 2)
        if morning_forecast_row
        else None
    )
    actual_hourly_rows = await db.fetchall(
        "SELECT target_hour, actual_kwh FROM hourly_predictions\n           WHERE target_date = ? AND actual_kwh IS NOT NULL",
        (today_str,),
    )
    cutoff_hour = latest_cutoff_hour
    actual_until_cutoff_kwh = 0.0
    if cutoff_hour is not None:
        for row in actual_hourly_rows:
            target_hour = int(row[0])
            if target_hour <= int(cutoff_hour):
                actual_until_cutoff_kwh += float(row[1] or 0.0)
    expected_day_kwh = None
    if (
        ops_daily_scope == "rest_of_day_total"
        and ops_daily_kwh is not None
        and (cutoff_hour is not None)
    ):
        expected_day_kwh = round(actual_until_cutoff_kwh + ops_daily_kwh, 2)
    elif morning_anchor_kwh is not None:
        expected_day_kwh = morning_anchor_kwh
    return (
        (
            (
                {
                    "total_kwh": round(total_kwh, 2),
                    "hourly_values": hourly_values,
                    "hourly_group_values": hourly_group_values,
                    "group_totals": group_totals,
                    "active_snapshot_types": sorted(snapshot_types),
                    "active_source_tracks": sorted(source_tracks),
                    "latest_snapshot_sequence": latest_snapshot_sequence,
                    "latest_trigger_source": latest_trigger_source,
                    "latest_cutoff_hour": latest_cutoff_hour,
                    "ops_today_row_kwh": ops_daily_kwh,
                    "ops_today_row_scope": ops_daily_scope,
                    "morning_anchor_kwh": morning_anchor_kwh,
                    "actual_until_cutoff_kwh": round(actual_until_cutoff_kwh, 2),
                    "expected_day_ist_plus_rest": expected_day_kwh,
                    "forecast_channel": (
                        "rest_of_day_ist_plus_sidecar"
                        if ops_daily_scope == "rest_of_day_total"
                        and expected_day_kwh is not None
                        else "morning_anchor"
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


def _resolve_hybrid_forecast_times(settings: dict[str, Any]) -> list[str]:
    forecast_mode = settings.get("mode") or OPERATIONAL_REFORECAST_MODE_STANDARD
    custom_time = settings.get("custom_time")
    if not settings.get("enabled", False):
        return []
    if forecast_mode == OPERATIONAL_REFORECAST_MODE_STANDARD:
        return []
    if forecast_mode == OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY:
        return ["12:00"]
    if forecast_mode == OPERATIONAL_REFORECAST_MODE_STANDARD_MIDDAY_AFTERNOON:
        return ["12:00", "16:00"]
    if forecast_mode == OPERATIONAL_REFORECAST_MODE_CUSTOM_TIME and custom_time:
        return [str(custom_time)]
    return []


def _next_hybrid_forecast_run(configured_times: list[str]) -> Optional[str]:
    if not configured_times:
        return
    from homeassistant.util import dt as dt_util

    now = dt_util.now()
    scheduled_runs = []
    for configured_time in configured_times:
        try:
            hour_text, minute_text = configured_time.split(":", 1)
            scheduled_run_at = now.replace(
                hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0
            )
            if scheduled_run_at <= now:
                scheduled_run_at = scheduled_run_at + timedelta(days=1)
            scheduled_runs.append(scheduled_run_at)
        except (TypeError, ValueError):
            continue
    if not scheduled_runs:
        return
    return min(scheduled_runs).isoformat()


class BaseSolarSensor(CoordinatorEntity, SensorEntity):
    """Base class for core sensors updated by the coordinator. @zara"""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the base sensor. @zara"""
        super().__init__(coordinator)
        self.entry = entry
        self._db = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Solar Forecast ML",
            manufacturer="Zara-Toorox",
            model=INTEGRATION_MODEL,
            sw_version=f"SW {SOFTWARE_VERSION} | AI {AI_VERSION}",
            configuration_url="https://github.com/Zara-Toorox/ha-solar-forecast-ml",
        )

    @property
    def db_manager(self) -> Optional[DatabaseManager]:
        """Get database manager from coordinator. @zara"""
        if hasattr(self.coordinator, "data_manager") and self.coordinator.data_manager:
            return self.coordinator.data_manager._db_manager
        return None if True else None

    @property
    def available(self) -> bool:
        """Return if entity is available based on coordinator. @zara"""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        )


class SolarForecastSensor(SensorEntity):
    """Sensor for today's or tomorrow's solar forecast using DB. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry, key: str
    ):
        """Initialize the forecast sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._key = key
        self._cached_tomorrow_value = 0.0
        self._ops_payload = None
        self._key_mapping = {
            "remaining": {
                "data_key": "prediction_kwh",
                "translation_key": "today_forecast",
            },
            "tomorrow": {
                "data_key": "forecast_tomorrow",
                "translation_key": "tomorrow_forecast",
            },
        }
        if key not in self._key_mapping:
            raise ValueError(
                f"Invalid sensor key: {key}. Must be 'remaining' or 'tomorrow'"
            )
        sensor_config = self._key_mapping[key]
        self._data_key = sensor_config["data_key"]
        self._attr_unique_id = f"{entry.entry_id}_ml_forecast_{key}"
        self._attr_translation_key = sensor_config["translation_key"]
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:solar-power"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def db_manager(self) -> Optional[DatabaseManager]:
        """Get database manager from coordinator. @zara"""
        if (
            hasattr(self._coordinator, "data_manager")
            and self._coordinator.data_manager
        ):
            return self._coordinator.data_manager._db_manager
        return None if True else None

    @property
    def available(self) -> bool:
        """Return if entity is available. @zara"""
        return True

    async def _load_tomorrow_from_db(self) -> None:
        """Load tomorrow forecast directly from daily_forecasts DB table. @zara"""
        try:
            db_manager = self._coordinator.data_manager._db_manager
            if not db_manager:
                return
            from homeassistant.util import dt as dt_util

            tomorrow_str = (dt_util.now() + timedelta(days=1)).date().isoformat()
            tomorrow_forecast_row = await db_manager.fetchone(
                "SELECT prediction_kwh FROM daily_forecasts\n                   WHERE forecast_type = 'tomorrow' AND forecast_date = ?",
                (tomorrow_str,),
            )
            if tomorrow_forecast_row and tomorrow_forecast_row[0] is not None:
                self._cached_tomorrow_value = round(float(tomorrow_forecast_row[0]), 2)
            elif self._coordinator.data:
                self._cached_tomorrow_value = (
                    self._coordinator.data.get(self._data_key) or 0.0
                )
        except Exception as error:
            _LOGGER.warning("Failed to load tomorrow forecast from DB: %s", error)
            if self._coordinator.data:
                self._cached_tomorrow_value = (
                    self._coordinator.data.get(self._data_key) or 0.0
                )
        return None if True else None

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        if value is None:
            return
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return
        return numeric_value if numeric_value >= 0.0 else None

    def _today_hourly_total(self) -> Optional[float]:
        try:
            from homeassistant.util import dt as dt_util

            hourly_prediction_cache = getattr(
                self._coordinator, CACHE_HOURLY_PREDICTIONS, None
            )
            if not hourly_prediction_cache or not isinstance(
                hourly_prediction_cache, dict
            ):
                return None
            predictions = hourly_prediction_cache.get(CACHE_PREDICTIONS, [])
            if not predictions:
                return
            today_str = dt_util.now().strftime("%Y-%m-%d")
            total_kwh = 0.0
            has_predictions = False
            for prediction in predictions:
                if prediction.get(PRED_TARGET_DATE) != today_str:
                    continue
                prediction_kwh = self._safe_float(
                    prediction.get(PRED_PREDICTION_KWH)
                    if prediction.get(PRED_PREDICTION_KWH) is not None
                    else prediction.get(PRED_PREDICTED_KWH)
                )
                if prediction_kwh is None:
                    continue
                has_predictions = True
                total_kwh += prediction_kwh
            return total_kwh if has_predictions else None
        except Exception as error:
            _LOGGER.warning(
                "Failed to calculate today's hourly forecast total: %s", error
            )
            return

    def _today_hourly_map(self) -> Optional[Dict[Any, float]]:
        try:
            from homeassistant.util import dt as dt_util

            hourly_prediction_cache = getattr(
                self._coordinator, CACHE_HOURLY_PREDICTIONS, None
            )
            if not hourly_prediction_cache or not isinstance(
                hourly_prediction_cache, dict
            ):
                return None
            predictions = hourly_prediction_cache.get(CACHE_PREDICTIONS, [])
            if not predictions:
                return
            today_str = dt_util.now().strftime("%Y-%m-%d")
            hourly_values = {}
            for prediction in predictions:
                if prediction.get(PRED_TARGET_DATE) != today_str:
                    continue
                target_hour = prediction.get(PRED_TARGET_HOUR)
                prediction_kwh = self._safe_float(
                    prediction.get(PRED_PREDICTION_KWH)
                    if prediction.get(PRED_PREDICTION_KWH) is not None
                    else prediction.get(PRED_PREDICTED_KWH)
                )
                if target_hour is None or prediction_kwh is None:
                    continue
                try:
                    hourly_values[int(target_hour)] = prediction_kwh
                except (TypeError, ValueError):
                    continue
            return hourly_values or None
        except Exception as error:
            _LOGGER.warning("Failed to build today's hourly forecast map: %s", error)
            return

    def _remaining_expected_inputs(
        self,
    ) -> tuple[Optional[dict], Optional[float], float]:
        hourly_values = None
        expected_day_kwh = None
        if self._ops_payload:
            hourly_values = self._ops_payload.get("hourly_values")
            expected_day_kwh = self._safe_float(
                self._ops_payload.get("expected_day_ist_plus_rest")
            )
            if expected_day_kwh is None:
                expected_day_kwh = self._safe_float(
                    self._ops_payload.get("morning_anchor_kwh")
                )
        if not hourly_values:
            hourly_values = self._today_hourly_map()
        if expected_day_kwh is None:
            expected_day_kwh = self._today_forecast_total()
        return (hourly_values, expected_day_kwh, self._today_actual_total())

    async def _load_today_ops_forecast(self) -> None:
        """Load active OPS forecast for today's operative forecast sensors."""
        self._ops_payload = None
        if self._key != "remaining":
            return
        try:
            from homeassistant.util import dt as dt_util

            db_manager = getattr(
                getattr(self._coordinator, "data_manager", None), "_db_manager", None
            )
            if db_manager is None or getattr(db_manager, "_db", None) is None:
                _LOGGER.debug("OPS forecast not loaded: database connection not open")
                return
            self._ops_payload = await _load_active_ops_day_forecast(
                db_manager, dt_util.now().date().isoformat()
            )
        except Exception as error:
            _LOGGER.warning(
                "Failed to load OPS forecast for today forecast sensor: %s", error
            )
            self._ops_payload = None
        return None if True else None

    def _today_forecast_total(self) -> float:
        if self._ops_payload:
            forecast_kwh = self._safe_float(self._ops_payload.get("total_kwh"))
            if forecast_kwh is not None:
                return forecast_kwh
        coordinator_data = self._coordinator.data or {}
        forecast_candidates = (
            getattr(self._coordinator, "expected_daily_production", None),
            coordinator_data.get(DATA_KEY_EXPECTED_DAILY_PRODUCTION),
            coordinator_data.get(DATA_KEY_FORECAST_TODAY),
            self._today_hourly_total(),
        )
        for forecast_candidate in forecast_candidates:
            forecast_kwh = self._safe_float(forecast_candidate)
            if not forecast_kwh is not None:
                continue
            return forecast_kwh
        return 0.0

    def _today_actual_total(self) -> float:
        try:
            from homeassistant.util import dt as dt_util

            actual_state_manager = getattr(
                self._coordinator, "actual_live_state_manager", None
            )
            actual_state = (
                getattr(actual_state_manager, "state", None)
                if actual_state_manager
                else None
            )
            if not isinstance(actual_state, dict):
                return 0.0
            state_date = actual_state.get("date")
            today_str = dt_util.now().date().isoformat()
            if state_date and state_date != today_str:
                return 0.0
            actual_kwh = self._safe_float(actual_state.get("total_actual_kwh"))
            return actual_kwh if actual_kwh is not None else 0.0
        except Exception as error:
            _LOGGER.warning(
                "Failed to read today's actual total for remaining forecast: %s", error
            )
            return 0.0

    @property
    def native_value(self) -> float:
        """Return remaining expected energy for the rest of today."""
        if self._key == "tomorrow":
            return self._cached_tomorrow_value
        from homeassistant.util import dt as dt_util

        hourly_values, expected_day_kwh, actual_today_kwh = (
            self._remaining_expected_inputs()
        )
        return remaining_today_expected_kwh(
            current_hour=dt_util.now().hour,
            hourly_values=hourly_values,
            expected_day_kwh=expected_day_kwh,
            actual_today_kwh=actual_today_kwh,
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Return hourly forecast breakdown. @zara"""
        if self._key == "remaining" and self._ops_payload:
            from homeassistant.util import dt as dt_util

            hourly_values, expected_day_kwh, actual_today_kwh = (
                self._remaining_expected_inputs()
            )
            remaining_forecast_kwh = remaining_today_expected_kwh(
                current_hour=dt_util.now().hour,
                hourly_values=hourly_values,
                expected_day_kwh=expected_day_kwh,
                actual_today_kwh=actual_today_kwh,
            )
            return {
                "forecast_source": "ops_active_forecast",
                "forecast_scope": "rest_of_day",
                "active_snapshot_types": self._ops_payload["active_snapshot_types"],
                "active_source_tracks": self._ops_payload["active_source_tracks"],
                "latest_snapshot_sequence": self._ops_payload[
                    "latest_snapshot_sequence"
                ],
                "latest_trigger_source": self._ops_payload["latest_trigger_source"],
                "latest_reforecast_cutoff_hour": self._ops_payload[
                    "latest_cutoff_hour"
                ],
                "ops_today_row_kwh": self._ops_payload["ops_today_row_kwh"],
                "ops_today_row_scope": self._ops_payload["ops_today_row_scope"],
                "morning_anchor_kwh": self._ops_payload.get("morning_anchor_kwh"),
                "actual_until_cutoff_kwh": self._ops_payload.get(
                    "actual_until_cutoff_kwh"
                ),
                "expected_day_ist_plus_rest": self._ops_payload.get(
                    "expected_day_ist_plus_rest"
                ),
                "expected_day_kwh": expected_day_kwh,
                "actual_today_kwh": round(actual_today_kwh, 3),
                "remaining_kwh": remaining_forecast_kwh,
                "forecast_channel": self._ops_payload.get("forecast_channel"),
                "hourly_forecast": self._ops_payload["hourly_values"],
                "hourly_panel_groups": self._ops_payload["hourly_group_values"],
                "panel_group_totals": self._ops_payload["group_totals"],
                "hours": self._ops_payload["hourly_values"],
            }
        hourly_prediction_cache = getattr(
            self._coordinator, CACHE_HOURLY_PREDICTIONS, None
        )
        if not hourly_prediction_cache:
            return {}
        if self._key == "tomorrow":
            return _build_hourly_attributes(
                hourly_prediction_cache.get(CACHE_PREDICTIONS_TOMORROW, [])
            )
        return _build_hourly_attributes(
            hourly_prediction_cache.get(CACHE_PREDICTIONS, [])
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass. @zara"""
        await super().async_added_to_hass()
        if self._key == "tomorrow":
            await self._load_tomorrow_from_db()
        else:
            await self._load_today_ops_forecast()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        if self._key == "remaining":
            actual_state_manager = getattr(
                self._coordinator, "actual_live_state_manager", None
            )
            add_actual_listener = (
                getattr(actual_state_manager, "add_listener", None)
                if actual_state_manager
                else None
            )
            if callable(add_actual_listener):
                self.async_on_remove(
                    add_actual_listener(self._handle_actual_live_update)
                )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        if self._key == "tomorrow":
            self.hass.async_create_task(self._reload_tomorrow_and_update())
        else:
            self.hass.async_create_task(self._reload_today_ops_and_update())
        return None if True else None

    async def _reload_tomorrow_and_update(self) -> None:
        """Reload tomorrow value from DB and update state. @zara"""
        await self._load_tomorrow_from_db()
        self.async_write_ha_state()

    async def _reload_today_ops_and_update(self) -> None:
        """Reload today's OPS forecast and update state. @zara"""
        await self._load_today_ops_forecast()
        self.async_write_ha_state()

    @callback
    def _handle_actual_live_update(self) -> None:
        """Handle SFML actual state updates. @zara"""
        self.hass.async_create_task(self._reload_today_ops_and_update())


class NextHourSensor(SensorEntity):
    """Sensor for the next hour's solar forecast from DB. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the next hour sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._upcoming_hours = []
        self._attr_unique_id = f"{entry.entry_id}_ml_next_hour_forecast"
        self._attr_translation_key = "next_hour_forecast"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:clock-fast"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Always available with fallback value. @zara"""
        return True

    @property
    def native_value(self) -> float:
        """Return next hour forecast or 0.0 if no data. @zara"""
        return self._cached_value if self._cached_value is not None else 0.0

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes showing all upcoming hours for the day. @zara"""
        if not self._upcoming_hours:
            return {}
        attributes = {}
        for hour_number, hour_forecast in enumerate(self._upcoming_hours, start=1):
            attributes[f"hour_{hour_number}"] = hour_forecast.get("kwh", 0.0)
            attributes[f"hour_{hour_number}_time"] = hour_forecast.get("time", "")
        upcoming_total_kwh = sum(
            (hour_forecast.get("kwh", 0.0) for hour_forecast in self._upcoming_hours)
        )
        attributes["total_upcoming"] = round(upcoming_total_kwh, 2)
        attributes["hours_count"] = len(self._upcoming_hours)
        attributes["hours_list"] = self._upcoming_hours
        return attributes

    async def _load_from_db(self) -> None:
        """Load next hour forecast from coordinator cache. @zara"""
        try:
            from homeassistant.util import dt as dt_util

            hourly_prediction_cache = getattr(
                self._coordinator, CACHE_HOURLY_PREDICTIONS, None
            )
            if not hourly_prediction_cache or not isinstance(
                hourly_prediction_cache, dict
            ):
                self._cached_value = None
                self._upcoming_hours = []
                return
            predictions = hourly_prediction_cache.get(CACHE_PREDICTIONS, [])
            if not predictions:
                self._cached_value = None
                self._upcoming_hours = []
                return
            now = dt_util.now()
            today_str = now.date().isoformat()
            current_hour = now.hour
            upcoming_predictions = [
                prediction
                for prediction in predictions
                if prediction.get(PRED_TARGET_DATE) == today_str
                if prediction.get(PRED_TARGET_HOUR, -1) > current_hour
            ]
            upcoming_predictions.sort(key=lambda p: p.get(PRED_TARGET_HOUR, 0))
            self._upcoming_hours = [
                {
                    "time": f"{prediction.get(PRED_TARGET_HOUR, 0):02d}:00",
                    "kwh": prediction.get(PRED_PREDICTION_KWH, 0.0),
                }
                for prediction in upcoming_predictions
            ]
            if upcoming_predictions:
                self._cached_value = upcoming_predictions[0].get(
                    PRED_PREDICTION_KWH, 0.0
                )
            else:
                self._cached_value = 0.0
        except Exception as error:
            _LOGGER.warning(f"Failed to load NextHourSensor: {error}")
            self._cached_value = None
            self._upcoming_hours = []
        return None if True else None

    async def async_added_to_hass(self) -> None:
        """Setup sensor with DB loading. @zara"""
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_db()
        self.async_write_ha_state()


class PeakProductionHourSensor(SensorEntity):
    """Sensor showing best production hour. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the peak hour sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._attr_unique_id = f"{entry.entry_id}_ml_peak_production_hour"
        self._attr_translation_key = "peak_production_hour"
        self._attr_icon = "mdi:solar-power-variant-outline"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Always available with fallback. @zara"""
        return True

    @property
    def native_value(self) -> str:
        """Return peak hour or '--:--' if no data. @zara"""
        return self._cached_value if self._cached_value is not None else "--:--"

    async def _load_from_db(self) -> None:
        """Load best hour from coordinator cache. @zara"""
        try:
            hourly_prediction_cache = getattr(
                self._coordinator, CACHE_HOURLY_PREDICTIONS, None
            )
            if not hourly_prediction_cache:
                self._cached_value = None
                return
            best_hour_forecast = hourly_prediction_cache.get(CACHE_BEST_HOUR_TODAY, {})
            if best_hour_forecast:
                target_hour = best_hour_forecast.get("hour")
                if target_hour is not None:
                    self._cached_value = f"{target_hour:02d}:00"
                    return
            self._cached_value = None
        except Exception as error:
            _LOGGER.warning(f"Failed to load PeakProductionHourSensor: {error}")
            self._cached_value = None
        return None if True else None

    async def async_added_to_hass(self) -> None:
        """Setup sensor. @zara"""
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_db()
        self.async_write_ha_state()


class AverageYieldSensor(BaseSolarSensor):
    """Sensor for the calculated average monthly yield. @zara"""

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the average yield sensor. @zara"""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ml_average_yield"
        self._attr_translation_key = "average_yield"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:chart-line"

    @property
    def native_value(self) -> Optional[float]:
        """Return the average monthly yield. @zara"""
        average_month_yield_kwh = getattr(self.coordinator, "avg_month_yield", None)
        return (
            average_month_yield_kwh
            if average_month_yield_kwh is not None and average_month_yield_kwh > 0
            else None
        )


class ExpectedDailyProductionSensor(SensorEntity):
    """Sensor for expected daily production morning snapshot using DB. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the expected daily production sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._forecast_source = "unknown"
        self._ops_payload = None
        self._attr_unique_id = f"{entry.entry_id}_ml_expected_daily_production"
        self._attr_translation_key = "expected_daily_production"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:solar-power-variant"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Sensor availability. @zara"""
        return self._cached_value is not None

    @property
    def native_value(self) -> Optional[float]:
        """Return cached value. @zara"""
        return self._cached_value

    @property
    def extra_state_attributes(self) -> dict:
        """Return hourly forecast breakdown for today. @zara"""
        if self._ops_payload:
            return {
                "forecast_source": self._forecast_source,
                "forecast_scope": "ops_full_day_active_sum",
                "active_snapshot_types": self._ops_payload["active_snapshot_types"],
                "active_source_tracks": self._ops_payload["active_source_tracks"],
                "latest_snapshot_sequence": self._ops_payload[
                    "latest_snapshot_sequence"
                ],
                "latest_trigger_source": self._ops_payload["latest_trigger_source"],
                "latest_reforecast_cutoff_hour": self._ops_payload[
                    "latest_cutoff_hour"
                ],
                "ops_today_row_kwh": self._ops_payload["ops_today_row_kwh"],
                "ops_today_row_scope": self._ops_payload["ops_today_row_scope"],
                "hourly_forecast": self._ops_payload["hourly_values"],
                "hourly_panel_groups": self._ops_payload["hourly_group_values"],
                "panel_group_totals": self._ops_payload["group_totals"],
                "hours": self._ops_payload["hourly_values"],
            }
        hourly_prediction_cache = getattr(
            self._coordinator, CACHE_HOURLY_PREDICTIONS, None
        )
        if not hourly_prediction_cache:
            return {"forecast_source": self._forecast_source}
        hourly_attributes = _build_hourly_attributes(
            hourly_prediction_cache.get(CACHE_PREDICTIONS, [])
        )
        hourly_attributes["forecast_source"] = self._forecast_source
        return hourly_attributes

    async def _load_from_db(self) -> None:
        """Load expected daily production from DB via coordinator. @zara"""
        self._forecast_source = "unknown"
        self._ops_payload = None
        try:
            from homeassistant.util import dt as dt_util

            db_manager = getattr(
                getattr(self._coordinator, "data_manager", None), "_db_manager", None
            )
            if db_manager:
                ops_forecast = await _load_active_ops_day_forecast(
                    db_manager, dt_util.now().date().isoformat()
                )
                if ops_forecast:
                    self._ops_payload = ops_forecast
                    self._cached_value = ops_forecast["total_kwh"]
                    self._forecast_source = "ops_active_forecast"
                    return
                coordinator_state = await db_manager.get_coordinator_state()
                if coordinator_state:
                    self._cached_value = coordinator_state.get(
                        DATA_KEY_EXPECTED_DAILY_PRODUCTION
                    )
                    self._forecast_source = "coordinator_state"
                    return
            if self._coordinator.data:
                self._cached_value = self._coordinator.data.get(
                    DATA_KEY_EXPECTED_DAILY_PRODUCTION
                )
                self._forecast_source = "coordinator_data"
        except Exception as error:
            _LOGGER.warning(f"Failed to load ExpectedDailyProductionSensor: {error}")
            self._cached_value = None
            self._forecast_source = "unavailable"
        return None if True else None

    async def async_added_to_hass(self) -> None:
        """Setup sensor with DB loading. @zara"""
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_db()
        self.async_write_ha_state()


class ConservativePlanningForecastSensor(SensorEntity):
    """Read-only conservative planning sensor blending SFML with TFS P10. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _sfml_weight = DEFAULT_SFML_WEIGHT
    _tfs_weight = DEFAULT_TFS_P10_WEIGHT

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the conservative planning forecast sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._hourly_values = {}
        self._hourly_group_values = {}
        self._group_totals = {}
        self._tfs_available = False
        self._tfs_client = None
        self._forecast_locked = False
        self._forecast_lock_source = None
        self._forecast_locked_at = None
        self._normal_full_day_forecast = None
        self._remaining_conservative_forecast = None
        self._remaining_forecast_cap = None
        self._mapping_mode = "unknown"
        self._mapping_fallback_hours = []
        self._p10_value_updated_at = None
        self._p10_age_hours = None
        self._p10_source = "unavailable"
        self._p10_persisted_fresh = False
        self._p10_recomputed_at = None
        self._tfs_generated_at = None
        self._tfs_http_status = None
        self._fallback_reason = None
        self._last_tfs_warning_at = None
        self._last_p10_log_key = None
        self._attr_unique_id = f"{entry.entry_id}_ml_conservative_planning_forecast"
        self._attr_translation_key = "conservative_planning_forecast"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:shield-sun"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Sensor availability. @zara"""
        return self._cached_value is not None

    @property
    def native_value(self) -> Optional[float]:
        """Return cached value. @zara"""
        return self._cached_value

    def _p10_diagnostic_attributes(self) -> dict:
        return {
            "p10_value_updated_at": self._p10_value_updated_at,
            "p10_age_hours": self._p10_age_hours,
            "p10_source": self._p10_source,
            "p10_persisted_fresh": self._p10_persisted_fresh,
            "p10_recomputed_at": self._p10_recomputed_at,
            "tfs_generated_at": self._tfs_generated_at,
            "tfs_http_status": self._tfs_http_status,
            "fallback_reason": self._fallback_reason,
        }

    @property
    def extra_state_attributes(self) -> dict:
        """Return hourly and per-group conservative planning values. @zara"""
        if self._cached_value is None:
            return {
                "blend_mode": "sfml_p50_tfs_p10_weighted",
                "blend_ratio": {"sfml": self._sfml_weight, "tfs_p10": self._tfs_weight},
                "tfs_available": self._tfs_available,
                "forecast_locked": self._forecast_locked,
                "forecast_lock_source": self._forecast_lock_source,
                "forecast_locked_at": self._forecast_locked_at,
                "forecast_scope": "full_day_conservative_sum",
                "normal_full_day_forecast": self._normal_full_day_forecast,
                "remaining_conservative_forecast": self._remaining_conservative_forecast,
                "remaining_forecast_cap": self._remaining_forecast_cap,
                "tfs_group_mapping_mode": self._mapping_mode,
                "tfs_group_mapping_fallback_hours": self._mapping_fallback_hours,
                **self._p10_diagnostic_attributes(),
                "hourly_forecast": {},
                "hourly_panel_groups": {},
                "panel_group_totals": {},
            }
        return {
            "blend_mode": "sfml_p50_tfs_p10_weighted",
            "blend_ratio": {"sfml": self._sfml_weight, "tfs_p10": self._tfs_weight},
            "tfs_available": self._tfs_available,
            "forecast_locked": self._forecast_locked,
            "forecast_lock_source": self._forecast_lock_source,
            "forecast_locked_at": self._forecast_locked_at,
            "forecast_scope": "full_day_conservative_sum",
            "normal_full_day_forecast": self._normal_full_day_forecast,
            "remaining_conservative_forecast": self._remaining_conservative_forecast,
            "remaining_forecast_cap": self._remaining_forecast_cap,
            "tfs_group_mapping_mode": self._mapping_mode,
            "tfs_group_mapping_fallback_hours": self._mapping_fallback_hours,
            **self._p10_diagnostic_attributes(),
            "hourly_forecast": self._hourly_values,
            "hourly_panel_groups": self._hourly_group_values,
            "panel_group_totals": self._group_totals,
            "hours": self._hourly_values,
        }

    def _parse_tfs_hourly_p10(
        self, response: Dict[str, Any]
    ) -> Dict[str, Dict[str, Any]]:
        """Parse TFS response into hour -> p10/group-p10 mapping. @zara"""
        hourly_quantiles = {}
        for hour_forecast in response.get("hours", []):
            hour_timestamp = hour_forecast.get("hour", "")
            if not hour_timestamp:
                continue
            hour_quantiles = {"p10": float(hour_forecast.get("p10", 0.0) or 0.0)}
            group_quantiles = hour_forecast.get("groups")
            if group_quantiles:
                hour_quantiles["groups"] = [
                    {
                        "name": group_forecast.get("name", ""),
                        "p10": float(group_forecast.get("p10", 0.0) or 0.0),
                    }
                    for group_forecast in group_quantiles
                ]
            hourly_quantiles[hour_timestamp] = hour_quantiles
        return hourly_quantiles

    async def _load_sfml_hourly_panel_groups(
        self, today_str: str
    ) -> list[Dict[str, Any]]:
        """Load today's SFML hourly predictions with panel groups directly from DB. @zara"""
        try:
            db_manager = getattr(
                getattr(self._coordinator, "data_manager", None), "_db_manager", None
            )
            if db_manager is None:
                return []
            group_forecast_rows = await db_manager.fetchall(
                "SELECT hp.target_hour, ppg.group_name, ppg.prediction_kwh\n                   FROM hourly_predictions hp\n                   JOIN prediction_panel_groups ppg ON ppg.prediction_id = hp.prediction_id\n                   WHERE hp.target_date = ?\n                   ORDER BY hp.target_hour, ppg.group_name",
                (today_str,),
            )
            if not group_forecast_rows:
                return []
            hourly_group_predictions = {}
            for row in group_forecast_rows:
                target_hour = int(row[0])
                group_name = str(row[1])
                prediction_kwh = float(row[2] or 0.0)
                if target_hour not in hourly_group_predictions:
                    hourly_group_predictions[target_hour] = {
                        PRED_TARGET_DATE: today_str,
                        PRED_TARGET_HOUR: target_hour,
                        "panel_group_predictions": {},
                    }
                hourly_group_predictions[target_hour]["panel_group_predictions"][
                    group_name
                ] = prediction_kwh
            return [
                hourly_group_predictions[target_hour]
                for target_hour in sorted(hourly_group_predictions)
            ]
            pass
        except Exception as error:
            _LOGGER.warning(
                "ConservativePlanningForecastSensor: failed to load SFML hourly panel groups: %s",
                error,
            )
            return []

    async def _load_today_forecast_lock_state(self, today_str: str) -> bool:
        """Load lock state from today's official SFML forecast row. @zara"""
        self._forecast_locked = False
        self._forecast_lock_source = None
        self._forecast_locked_at = None
        try:
            db_manager = getattr(
                getattr(self._coordinator, "data_manager", None), "_db_manager", None
            )
            if db_manager is None:
                return False
            forecast_lock_row = await db_manager.fetchone(
                "SELECT locked, source, locked_at\n                   FROM daily_forecasts\n                   WHERE forecast_type = 'today' AND forecast_date = ?\n                   LIMIT 1",
                (today_str,),
            )
            if not forecast_lock_row:
                return False
            self._forecast_locked = bool(forecast_lock_row[0])
            self._forecast_lock_source = (
                str(forecast_lock_row[1]) if forecast_lock_row[1] is not None else None
            )
            self._forecast_locked_at = (
                str(forecast_lock_row[2]) if forecast_lock_row[2] is not None else None
            )
            return self._forecast_locked
        except Exception as error:
            _LOGGER.warning(
                "ConservativePlanningForecastSensor: failed to load forecast lock state: %s",
                error,
            )
            return False

    def _map_tfs_groups(
        self,
        tfs_group_list: list[Dict[str, Any]],
        sfml_group_predictions: Dict[str, float],
        hour_label: str,
    ) -> Dict[str, float]:
        """Map TFS group values onto SFML group names. @zara"""
        sfml_group_names = list(sfml_group_predictions.keys())
        tfs_p10_by_group = {}
        has_duplicate_group_names = False
        has_named_groups = False
        for group_forecast in tfs_group_list:
            group_name = str(group_forecast.get("name", "")).strip()
            if group_name:
                has_named_groups = True
                if group_name in tfs_p10_by_group:
                    has_duplicate_group_names = True
                    continue
                tfs_p10_by_group[group_name] = float(
                    group_forecast.get("p10", 0.0) or 0.0
                )
        if tfs_p10_by_group:
            if not has_duplicate_group_names:
                mapped_group_p10 = {
                    group_name: tfs_p10_by_group[group_name]
                    for group_name in sfml_group_names
                    if group_name in tfs_p10_by_group
                }
                if len(mapped_group_p10) == len(sfml_group_names):
                    self._mapping_mode = "name"
                    return mapped_group_p10
                return {}
        if has_named_groups:
            return {}
        if len(tfs_group_list) == len(sfml_group_names):
            self._mapping_mode = "index_fallback"
            self._mapping_fallback_hours.append(hour_label)
            return {
                group_name: float(tfs_group_list[group_index].get("p10", 0.0) or 0.0)
                for group_index, group_name in enumerate(sfml_group_names)
            }
        return {}

    def _today_actual_total(self, today_str: str) -> float:
        try:
            actual_state_manager = getattr(
                self._coordinator, "actual_live_state_manager", None
            )
            actual_state = (
                getattr(actual_state_manager, "state", None)
                if actual_state_manager
                else None
            )
            if not isinstance(actual_state, dict):
                return 0.0
            state_date = actual_state.get("date")
            if state_date and state_date != today_str:
                return 0.0
            actual_kwh = actual_state.get("total_actual_kwh")
            if actual_kwh is None:
                return 0.0
            return max(0.0, float(actual_kwh))
        except Exception:
            return 0.0

    async def _load_current_forecast_caps(
        self, today_str: str
    ) -> tuple[Optional[float], Optional[float]]:
        db_manager = getattr(
            getattr(self._coordinator, "data_manager", None), "_db_manager", None
        )
        if db_manager is None:
            return (None, None)
        full_day_cap = None
        ops_forecast = await _load_active_ops_day_forecast(db_manager, today_str)
        if ops_forecast:
            full_day_cap = float(ops_forecast["total_kwh"])
        else:
            daily_forecast_row = await db_manager.fetchone(
                "SELECT prediction_kwh\n                   FROM daily_forecasts\n                   WHERE forecast_type = 'today' AND forecast_date = ?\n                   LIMIT 1",
                (today_str,),
            )
            if daily_forecast_row and daily_forecast_row[0] is not None:
                full_day_cap = float(daily_forecast_row[0])
        if full_day_cap is None:
            daily_forecast_row = await db_manager.fetchone(
                "SELECT SUM(ppg.prediction_kwh)\n                   FROM hourly_predictions hp\n                   JOIN prediction_panel_groups ppg ON ppg.prediction_id = hp.prediction_id\n                   WHERE hp.target_date = ?",
                (today_str,),
            )
            if daily_forecast_row and daily_forecast_row[0] is not None:
                full_day_cap = float(daily_forecast_row[0])
        if full_day_cap is None:
            return (None, None)
        full_day_cap = max(0.0, full_day_cap)
        remaining_cap = max(0.0, full_day_cap - self._today_actual_total(today_str))
        return (round(full_day_cap, 2), round(remaining_cap, 2))

    def _log_p10_event_once(
        self, level: int, key: tuple, message: str, *args: Any
    ) -> None:
        if self._last_p10_log_key == key:
            return
        self._last_p10_log_key = key
        _LOGGER.log(level, message, *args)

    def _log_tfs_fallback(self, message: str, *args: Any) -> None:
        now = datetime.now()
        if self._last_tfs_warning_at and now - self._last_tfs_warning_at < timedelta(
            hours=1
        ):
            _LOGGER.debug(message, *args)
            return
        self._last_tfs_warning_at = now
        _LOGGER.warning(message, *args)

    async def _clear_persisted_p10_value(self, today_str: str) -> None:
        try:
            db_manager = getattr(
                getattr(self._coordinator, "data_manager", None), "_db_manager", None
            )
            if db_manager is None:
                return
            await db_manager.execute(
                "UPDATE daily_forecast_tracking\n                   SET conservative_planning_forecast_kwh = NULL,\n                       conservative_planning_forecast_updated_at = NULL,\n                       conservative_planning_forecast_hours_json = NULL,\n                       conservative_planning_forecast_panel_groups_json = NULL,\n                       conservative_planning_forecast_group_totals_json = NULL\n                   WHERE id = 1 AND date = ?",
                (today_str,),
            )
        except Exception as error:
            _LOGGER.debug(
                "Could not clear stale conservative planning value: %s", error
            )
        return None if True else None

    async def _load_persisted_value(
        self,
        today_str: str,
        full_day_cap: Optional[float] = None,
        remaining_cap: Optional[float] = None,
    ) -> Optional[float]:
        """Load persisted conservative planning value for today. @zara"""
        try:
            db_manager = getattr(
                getattr(self._coordinator, "data_manager", None), "_db_manager", None
            )
            if db_manager is None:
                return
            persisted_forecast_row = await db_manager.fetchone(
                "SELECT conservative_planning_forecast_kwh,\n                          conservative_planning_forecast_updated_at,\n                          conservative_planning_forecast_hours_json,\n                          conservative_planning_forecast_panel_groups_json,\n                          conservative_planning_forecast_group_totals_json\n                   FROM daily_forecast_tracking\n                   WHERE id = 1 AND date = ?",
                (today_str,),
            )
            if persisted_forecast_row:
                if persisted_forecast_row[0] is not None:
                    freshness = persisted_p10_freshness(
                        today_str, persisted_forecast_row[1]
                    )
                    self._p10_persisted_fresh = bool(freshness["fresh"])
                    self._p10_value_updated_at = (
                        str(persisted_forecast_row[1])
                        if persisted_forecast_row[1] is not None
                        else None
                    )
                    self._p10_age_hours = freshness.get("age_hours")
                    if not self._p10_persisted_fresh:
                        self._fallback_reason = freshness["reason"]
                        self._p10_source = "stale_persisted_p10"
                        self._log_p10_event_once(
                            logging.INFO,
                            (
                                "stale_persisted_p10",
                                today_str,
                                self._p10_value_updated_at,
                            ),
                            "ConservativePlanningForecastSensor: discarded stale_persisted_p10 date=%s p10_updated_at=%s age_hours=%s forecast_date=%s",
                            today_str,
                            self._p10_value_updated_at,
                            self._p10_age_hours,
                            today_str,
                        )
                        await self._clear_persisted_p10_value(today_str)
                        return
                    self._hourly_values = (
                        json.loads(persisted_forecast_row[2])
                        if persisted_forecast_row[2]
                        else {}
                    )
                    self._hourly_group_values = (
                        json.loads(persisted_forecast_row[3])
                        if persisted_forecast_row[3]
                        else {}
                    )
                    self._group_totals = (
                        json.loads(persisted_forecast_row[4])
                        if persisted_forecast_row[4]
                        else {}
                    )
                    self._tfs_available = bool(self._hourly_values)
                    persisted_forecast_kwh = cap_persisted_conservative_value(
                        persisted_forecast_row[0], full_day_cap
                    )
                    self._cached_value = persisted_forecast_kwh
                    self._remaining_conservative_forecast = (
                        remaining_conservative_value(
                            persisted_forecast_kwh,
                            self._today_actual_total(today_str),
                            remaining_cap,
                        )
                    )
                    self._p10_source = "persisted"
                    self._fallback_reason = None
                    self._log_p10_event_once(
                        logging.DEBUG,
                        (
                            "fresh_persisted_p10",
                            today_str,
                            self._p10_value_updated_at,
                            persisted_forecast_kwh,
                        ),
                        "ConservativePlanningForecastSensor: using persisted P10 value=%.2f date=%s updated_at=%s age_hours=%s source=persisted",
                        persisted_forecast_kwh,
                        today_str,
                        self._p10_value_updated_at,
                        self._p10_age_hours,
                    )
                    return persisted_forecast_kwh
        except Exception as error:
            _LOGGER.warning(
                "Failed to load persisted conservative planning value: %s", error
            )
            return
        return None if True else None

    async def _persist_final_value(self, today_str: str, value: float) -> None:
        """Persist final conservative value plus planning attributes. @zara"""
        try:
            from homeassistant.util import dt as dt_util

            db_manager = getattr(
                getattr(self._coordinator, "data_manager", None), "_db_manager", None
            )
            if db_manager is None:
                return
            recomputed_at = dt_util.now().isoformat()
            p10_updated_at = recomputed_at
            locked_at_timestamp = parse_p10_timestamp(self._forecast_locked_at)
            if (
                self._forecast_lock_source == "morning_routine"
                and locked_at_timestamp is not None
                and (locked_at_timestamp.date().isoformat() == today_str)
            ):
                p10_updated_at = str(self._forecast_locked_at)
            self._p10_value_updated_at = p10_updated_at
            self._p10_age_hours = p10_age_hours(p10_updated_at)
            self._p10_persisted_fresh = True
            self._p10_recomputed_at = recomputed_at
            await db_manager.execute(
                "INSERT INTO daily_forecast_tracking\n                   (id, date, conservative_planning_forecast_kwh,\n                    conservative_planning_forecast_updated_at,\n                    conservative_planning_forecast_hours_json,\n                    conservative_planning_forecast_panel_groups_json,\n                    conservative_planning_forecast_group_totals_json,\n                    last_updated)\n                   VALUES (1, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       date = excluded.date,\n                       conservative_planning_forecast_kwh = excluded.conservative_planning_forecast_kwh,\n                       conservative_planning_forecast_updated_at = excluded.conservative_planning_forecast_updated_at,\n                       conservative_planning_forecast_hours_json = excluded.conservative_planning_forecast_hours_json,\n                       conservative_planning_forecast_panel_groups_json = excluded.conservative_planning_forecast_panel_groups_json,\n                       conservative_planning_forecast_group_totals_json = excluded.conservative_planning_forecast_group_totals_json,\n                       last_updated = excluded.last_updated",
                (
                    today_str,
                    round(value, 2),
                    p10_updated_at,
                    json.dumps(self._hourly_values),
                    json.dumps(self._hourly_group_values),
                    json.dumps(self._group_totals),
                    recomputed_at,
                ),
            )
        except Exception as error:
            _LOGGER.warning("Failed to persist conservative planning value: %s", error)
        return None if True else None

    async def _fetch_tfs_quantiles(self) -> Optional[Dict[str, Any]]:
        """Fetch TFS quantiles directly from the dedicated endpoint. @zara"""
        try:
            if self.hass is None:
                return
            tfs_base_url = getattr(
                self._tfs_client, "_base_url", "http://127.0.0.1:8780"
            )
            http_session = async_get_clientsession(self.hass)
            async with http_session.get(
                f"{tfs_base_url}/api/forecast/quantiles", timeout=30
            ) as response:
                self._tfs_http_status = response.status
                if response.status != 200:
                    self._fallback_reason = f"tfs_http_{response.status}"
                    self._log_tfs_fallback(
                        "ConservativePlanningForecastSensor: TFS quantiles unavailable (HTTP %s); using SFML-only fallback",
                        response.status,
                    )
                    return
                quantile_payload = await response.json()
                self._tfs_generated_at = quantile_payload.get("generated_at")
                return quantile_payload
        except Exception as error:
            self._tfs_http_status = None
            self._fallback_reason = "tfs_exception"
            self._log_tfs_fallback(
                "ConservativePlanningForecastSensor: TFS quantiles unavailable; using SFML-only fallback: %s",
                error,
            )
            return
        return None if True else None

    async def _load_from_sources(self) -> None:
        """Load and blend conservative planning values from SFML cache and TFS. @zara"""
        self._cached_value = None
        self._hourly_values = {}
        self._hourly_group_values = {}
        self._group_totals = {}
        self._tfs_available = False
        self._forecast_locked = False
        self._forecast_lock_source = None
        self._forecast_locked_at = None
        self._normal_full_day_forecast = None
        self._remaining_conservative_forecast = None
        self._remaining_forecast_cap = None
        self._mapping_mode = "unknown"
        self._mapping_fallback_hours = []
        self._p10_value_updated_at = None
        self._p10_age_hours = None
        self._p10_source = "unavailable"
        self._p10_persisted_fresh = False
        self._p10_recomputed_at = None
        self._tfs_generated_at = None
        self._tfs_http_status = None
        self._fallback_reason = None
        try:
            from homeassistant.util import dt as dt_util

            today_str = dt_util.now().date().isoformat()
            forecast_locked = await self._load_today_forecast_lock_state(today_str)
            full_day_cap, remaining_cap = await self._load_current_forecast_caps(
                today_str
            )
            self._normal_full_day_forecast = full_day_cap
            self._remaining_forecast_cap = remaining_cap
            persisted_forecast_kwh = await self._load_persisted_value(
                today_str, full_day_cap, remaining_cap
            )
            if (
                forecast_locked
                and persisted_forecast_kwh is not None
                and self._p10_persisted_fresh
            ):
                self._cached_value = persisted_forecast_kwh
                return
            if not forecast_locked:
                self._cached_value = persisted_forecast_kwh
                return
            if not p10_morning_recompute_allowed(
                today_str, self._forecast_lock_source, self._forecast_locked_at
            ):
                self._p10_source = "missing_morning_snapshot"
                if self._fallback_reason is None:
                    self._fallback_reason = "outside_morning_snapshot_window"
                self._log_p10_event_once(
                    logging.DEBUG,
                    (
                        "p10_recompute_blocked",
                        today_str,
                        self._forecast_lock_source,
                        self._forecast_locked_at,
                    ),
                    "P10 planning value kept from the morning snapshot for %s (recomputed only inside the morning window; lock_source=%s, locked_at=%s, reason=%s)",
                    today_str,
                    self._forecast_lock_source,
                    self._forecast_locked_at,
                    self._fallback_reason,
                )
                return
            sfml_hourly_forecasts = await self._load_sfml_hourly_panel_groups(today_str)
            if not sfml_hourly_forecasts:
                self._cached_value = persisted_forecast_kwh
                return
            if self._tfs_client is None:
                self._tfs_client = TFSClient(self.hass)
            tfs_quantile_payload = await self._fetch_tfs_quantiles()
            tfs_hourly_quantiles = (
                self._parse_tfs_hourly_p10(tfs_quantile_payload)
                if tfs_quantile_payload
                else {}
            )
            has_positive_p10 = any(
                (
                    (hour_quantiles.get("p10") or 0.0) > 0
                    or any(
                        (
                            (group_quantiles.get("p10") or 0.0) > 0
                            for group_quantiles in hour_quantiles.get("groups", [])
                        )
                    )
                    for hour_quantiles in tfs_hourly_quantiles.values()
                )
            )
            if tfs_quantile_payload and (not has_positive_p10):
                self._fallback_reason = "tfs_response_without_p10"
                _LOGGER.warning(
                    "ConservativePlanningForecastSensor: TFS response contains no p10 values for forecast_type=sfml"
                )
            elif not tfs_quantile_payload and self._fallback_reason is None:
                self._fallback_reason = "tfs_unavailable"
            hourly_forecast_kwh = {}
            hourly_group_values = {}
            group_totals = {}
            total_forecast_kwh = 0.0
            for hourly_prediction in sfml_hourly_forecasts:
                if hourly_prediction.get(PRED_TARGET_DATE) != today_str:
                    continue
                target_hour = hourly_prediction.get(PRED_TARGET_HOUR)
                raw_group_predictions = (
                    hourly_prediction.get("panel_group_predictions") or {}
                )
                if target_hour is None or not raw_group_predictions:
                    continue
                sfml_group_predictions = {
                    str(group_name): float(raw_prediction_kwh or 0.0)
                    for group_name, raw_prediction_kwh in raw_group_predictions.items()
                }
                hour_label = f"{int(target_hour):02d}:00"
                hour_timestamp = f"{today_str}T{int(target_hour):02d}:00:00"
                hour_quantiles = tfs_hourly_quantiles.get(hour_timestamp)
                tfs_group_quantiles = (
                    hour_quantiles.get("groups") if hour_quantiles else None
                )
                tfs_group_predictions = {}
                if tfs_group_quantiles:
                    tfs_group_predictions = self._map_tfs_groups(
                        tfs_group_quantiles, sfml_group_predictions, hour_label
                    )
                    if not tfs_group_predictions:
                        self._mapping_mode = "sfml_only_fallback"
                        self._mapping_fallback_hours.append(hour_label)
                elif self._mapping_mode == "unknown":
                    self._mapping_mode = "sfml_only_fallback"
                blended_group_predictions = {}
                for group_name, sfml_prediction_kwh in sfml_group_predictions.items():
                    blended_prediction_kwh = conservative_planning_value(
                        sfml_prediction_kwh,
                        tfs_group_predictions.get(group_name),
                        sfml_weight=self._sfml_weight,
                        tfs_weight=self._tfs_weight,
                        precision=4,
                    )
                    blended_group_predictions[group_name] = blended_prediction_kwh
                    group_totals[group_name] = round(
                        group_totals.get(group_name, 0.0) + blended_prediction_kwh, 4
                    )
                if not blended_group_predictions:
                    continue
                sfml_hourly_cap_kwh = sum(
                    (
                        max(0.0, sfml_prediction_kwh)
                        for sfml_prediction_kwh in sfml_group_predictions.values()
                    )
                )
                total_forecast_kwh += sfml_hourly_cap_kwh
                blended_hour_kwh = round_not_above(
                    sum(blended_group_predictions.values()), sfml_hourly_cap_kwh, 4
                )
                hourly_forecast_kwh[hour_label] = blended_hour_kwh
                hourly_group_values[hour_label] = blended_group_predictions
            if not hourly_forecast_kwh:
                return
            self._tfs_available = has_positive_p10
            self._p10_source = (
                "morning_snapshot_tfs"
                if has_positive_p10
                else "morning_snapshot_sfml_fallback"
            )
            if has_positive_p10:
                self._fallback_reason = None
            if self._mapping_mode == "unknown":
                self._mapping_mode = (
                    "name" if has_positive_p10 else "sfml_only_fallback"
                )
            conservative_forecast_kwh = round(total_forecast_kwh, 2)
            self._normal_full_day_forecast = (
                min(self._normal_full_day_forecast, conservative_forecast_kwh)
                if self._normal_full_day_forecast is not None
                else conservative_forecast_kwh
            )
            self._cached_value = round_not_above(
                sum(hourly_forecast_kwh.values()), conservative_forecast_kwh, 2
            )
            self._remaining_conservative_forecast = remaining_conservative_value(
                self._cached_value,
                self._today_actual_total(today_str),
                self._remaining_forecast_cap,
            )
            self._hourly_values = {
                hour_label: round(forecast_kwh, 3)
                for hour_label, forecast_kwh in hourly_forecast_kwh.items()
            }
            self._hourly_group_values = {
                hour_label: {
                    group_name: round(forecast_kwh, 4)
                    for group_name, forecast_kwh in group_predictions.items()
                }
                for hour_label, group_predictions in hourly_group_values.items()
            }
            self._group_totals = {
                group_name: round(forecast_kwh, 3)
                for group_name, forecast_kwh in group_totals.items()
            }
            await self._persist_final_value(today_str, self._cached_value)
            self._log_p10_event_once(
                logging.INFO,
                ("recomputed_p10", today_str, self._cached_value, self._p10_source),
                "ConservativePlanningForecastSensor: recomputed P10 value=%.2f date=%s source=%s tfs_generated_at=%s fallback_reason=%s",
                self._cached_value,
                today_str,
                self._p10_source,
                self._tfs_generated_at,
                self._fallback_reason,
            )
        except Exception as error:
            _LOGGER.warning(
                "Failed to load ConservativePlanningForecastSensor: %s", error
            )
            try:
                from homeassistant.util import dt as dt_util

                fallback_today_str = dt_util.now().date().isoformat()
                await self._load_today_forecast_lock_state(fallback_today_str)
                self._cached_value = await self._load_persisted_value(
                    fallback_today_str
                )
            except Exception:
                self._cached_value = None
            self._hourly_values = {}
            self._hourly_group_values = {}
            self._group_totals = {}
            self._tfs_available = False
        if True:
            return None
        else:
            return None

    async def async_added_to_hass(self) -> None:
        """Setup sensor with coordinator listener. @zara"""
        await super().async_added_to_hass()
        self._tfs_client = TFSClient(self.hass)
        await self._load_from_sources()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_sources()
        self.async_write_ha_state()


class HybridForecastStatusSensor(SensorEntity):
    """Read-only status view for scheduled Hybrid Forecast configuration."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        self._coordinator = coordinator
        self.entry = entry
        self._state = "unknown"
        self._attributes = {}
        self._attr_unique_id = f"{entry.entry_id}_ml_hybrid_forecast_status"
        self._attr_translation_key = "hybrid_forecast_status"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        return self._state != "unknown"

    @property
    def native_value(self) -> str:
        return self._state

    @property
    def extra_state_attributes(self) -> dict:
        return self._attributes

    async def _load_status(self) -> None:
        default_settings = {
            "mode": OPERATIONAL_REFORECAST_MODE_STANDARD,
            "custom_time": None,
            "enabled": False,
            "updated_at": None,
            "updated_by": None,
            "notes": None,
        }
        hybrid_settings = default_settings
        latest_snapshot_row = None
        try:
            data_manager = getattr(self._coordinator, "data_manager", None)
            operational_handler = getattr(
                data_manager, "operational_forecast_handler", None
            )
            if operational_handler is not None:
                hybrid_settings = await operational_handler.get_reforecast_settings()
            db_manager = getattr(data_manager, "_db_manager", None)
            if db_manager is not None:
                latest_snapshot_row = await db_manager.fetchone(
                    "SELECT snapshot_sequence, status, triggered_at, cutoff_hour,\n                              trigger_source, snapshot_type\n                       FROM ops_forecast_snapshots\n                       WHERE trigger_source = 'scheduled_mode'\n                         AND snapshot_type = 'intraday_reforecast'\n                       ORDER BY snapshot_sequence DESC\n                       LIMIT 1"
                )
            configured_times = _resolve_hybrid_forecast_times(hybrid_settings)
            hybrid_enabled = bool(hybrid_settings.get("enabled", False))
            forecast_mode = (
                hybrid_settings.get("mode") or OPERATIONAL_REFORECAST_MODE_STANDARD
            )
            self._state = "disabled" if not hybrid_enabled else str(forecast_mode)
            latest_snapshot_sequence = None
            last_status = None
            last_run_at = None
            last_cutoff_hour = None
            last_trigger_source = None
            last_snapshot_type = None
            if latest_snapshot_row is not None:
                latest_snapshot_sequence = latest_snapshot_row[0]
                last_status = latest_snapshot_row[1]
                last_run_at = latest_snapshot_row[2]
                last_cutoff_hour = latest_snapshot_row[3]
                last_trigger_source = latest_snapshot_row[4]
                last_snapshot_type = latest_snapshot_row[5]
            self._attributes = {
                "enabled": hybrid_enabled,
                "mode": forecast_mode,
                "configured_times": configured_times,
                "custom_time": hybrid_settings.get("custom_time"),
                "next_run": _next_hybrid_forecast_run(configured_times),
                "last_run_at": last_run_at,
                "last_trigger_source": last_trigger_source,
                "last_cutoff_hour": last_cutoff_hour,
                "last_snapshot_sequence": latest_snapshot_sequence,
                "last_snapshot_type": last_snapshot_type,
                "last_status": last_status,
                "updated_at": hybrid_settings.get("updated_at"),
                "updated_by": hybrid_settings.get("updated_by"),
            }
        except Exception as error:
            _LOGGER.warning("Failed to load HybridForecastStatusSensor: %s", error)
            self._state = "unknown"
            self._attributes = {}
        return None if True else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._load_status()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        await self._load_status()
        self.async_write_ha_state()


class OperationalHybridForecastSensor(RestoreEntity, SensorEntity):
    """Read-only operational hybrid forecast sensor backed by the OPS track. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the operational hybrid forecast sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._hourly_values = {}
        self._hourly_group_values = {}
        self._group_totals = {}
        self._active_snapshot_types = []
        self._active_source_tracks = []
        self._latest_snapshot_sequence = None
        self._latest_trigger_source = None
        self._latest_cutoff_hour = None
        self._ops_today_row_kwh = None
        self._ops_today_row_scope = None
        self._morning_anchor_kwh = None
        self._actual_until_cutoff_kwh = None
        self._expected_day_ist_plus_rest = None
        self._forecast_channel = None
        self._attr_unique_id = f"{entry.entry_id}_ml_operational_hybrid_forecast"
        self._attr_translation_key = "hybrid_forecast"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:solar-power-variant-outline"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Sensor availability. @zara"""
        return self._cached_value is not None

    @property
    def native_value(self) -> Optional[float]:
        """Return the current operational hybrid forecast total. @zara"""
        return self._cached_value

    @property
    def extra_state_attributes(self) -> dict:
        """Return OPS-backed hourly and panel-group attributes. @zara"""
        return {
            "forecast_scope": "full_day_operational_sum",
            "active_snapshot_types": self._active_snapshot_types,
            "active_source_tracks": self._active_source_tracks,
            "latest_snapshot_sequence": self._latest_snapshot_sequence,
            "latest_trigger_source": self._latest_trigger_source,
            "latest_reforecast_cutoff_hour": self._latest_cutoff_hour,
            "ops_today_row_kwh": self._ops_today_row_kwh,
            "ops_today_row_scope": self._ops_today_row_scope,
            "morning_anchor_kwh": self._morning_anchor_kwh,
            "actual_until_cutoff_kwh": self._actual_until_cutoff_kwh,
            "expected_day_ist_plus_rest": self._expected_day_ist_plus_rest,
            "forecast_channel": self._forecast_channel,
            "hourly_forecast": self._hourly_values,
            "hourly_panel_groups": self._hourly_group_values,
            "panel_group_totals": self._group_totals,
            "hours": self._hourly_values,
        }

    async def _load_from_ops(self) -> None:
        """Load the operational hybrid forecast from active OPS rows. @zara"""
        self._cached_value = None
        self._hourly_values = {}
        self._hourly_group_values = {}
        self._group_totals = {}
        self._active_snapshot_types = []
        self._active_source_tracks = []
        self._latest_snapshot_sequence = None
        self._latest_trigger_source = None
        self._latest_cutoff_hour = None
        self._ops_today_row_kwh = None
        self._ops_today_row_scope = None
        self._morning_anchor_kwh = None
        self._actual_until_cutoff_kwh = None
        self._expected_day_ist_plus_rest = None
        self._forecast_channel = None
        try:
            from homeassistant.util import dt as dt_util

            db_manager = getattr(
                getattr(self._coordinator, "data_manager", None), "_db_manager", None
            )
            if db_manager is None:
                return
            today_str = dt_util.now().date().isoformat()
            ops_forecast = await _load_active_ops_day_forecast(db_manager, today_str)
            if not ops_forecast:
                return
            self._hourly_values = ops_forecast["hourly_values"]
            self._hourly_group_values = ops_forecast["hourly_group_values"]
            self._group_totals = ops_forecast["group_totals"]
            self._active_snapshot_types = ops_forecast["active_snapshot_types"]
            self._active_source_tracks = ops_forecast["active_source_tracks"]
            self._latest_snapshot_sequence = ops_forecast["latest_snapshot_sequence"]
            self._latest_trigger_source = ops_forecast["latest_trigger_source"]
            self._latest_cutoff_hour = ops_forecast["latest_cutoff_hour"]
            self._ops_today_row_kwh = ops_forecast["ops_today_row_kwh"]
            self._ops_today_row_scope = ops_forecast["ops_today_row_scope"]
            self._morning_anchor_kwh = ops_forecast.get("morning_anchor_kwh")
            self._actual_until_cutoff_kwh = ops_forecast.get("actual_until_cutoff_kwh")
            self._expected_day_ist_plus_rest = ops_forecast.get(
                "expected_day_ist_plus_rest"
            )
            self._forecast_channel = ops_forecast.get("forecast_channel")
            self._cached_value = ops_forecast["total_kwh"]
        except Exception as error:
            _LOGGER.warning("Failed to load OperationalHybridForecastSensor: %s", error)
            self._cached_value = None
        return None if True else None

    async def async_added_to_hass(self) -> None:
        """Setup sensor with coordinator listener. @zara"""
        await super().async_added_to_hass()
        await self._load_from_ops()
        if self._cached_value is None:
            previous_state = await self.async_get_last_state()
            if previous_state is not None:
                try:
                    if previous_state.state not in (None, "", "unknown", "unavailable"):
                        self._cached_value = round(float(previous_state.state), 2)
                    previous_attributes = previous_state.attributes or {}
                    self._hourly_values = dict(
                        previous_attributes.get("hourly_forecast", {})
                    )
                    self._hourly_group_values = dict(
                        previous_attributes.get("hourly_panel_groups", {})
                    )
                    self._group_totals = dict(
                        previous_attributes.get("panel_group_totals", {})
                    )
                    self._active_snapshot_types = list(
                        previous_attributes.get("active_snapshot_types", [])
                    )
                    self._active_source_tracks = list(
                        previous_attributes.get("active_source_tracks", [])
                    )
                    self._latest_snapshot_sequence = previous_attributes.get(
                        "latest_snapshot_sequence"
                    )
                    self._latest_trigger_source = previous_attributes.get(
                        "latest_trigger_source"
                    )
                    self._latest_cutoff_hour = previous_attributes.get(
                        "latest_reforecast_cutoff_hour"
                    )
                    self._ops_today_row_kwh = previous_attributes.get(
                        "ops_today_row_kwh"
                    )
                    self._ops_today_row_scope = previous_attributes.get(
                        "ops_today_row_scope"
                    )
                    self._morning_anchor_kwh = previous_attributes.get(
                        "morning_anchor_kwh"
                    )
                    self._actual_until_cutoff_kwh = previous_attributes.get(
                        "actual_until_cutoff_kwh"
                    )
                    self._expected_day_ist_plus_rest = previous_attributes.get(
                        "expected_day_ist_plus_rest"
                    )
                    self._forecast_channel = previous_attributes.get("forecast_channel")
                    _LOGGER.info(
                        "OperationalHybridForecastSensor restored previous state from HA storage"
                    )
                except Exception as error:
                    _LOGGER.warning(
                        "Failed to restore OperationalHybridForecastSensor state: %s",
                        error,
                    )
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_ops()
        self.async_write_ha_state()


class ProductionTimeSensor(SensorEntity):
    """Sensor for production time today from DB. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the production time sensor. @zara"""
        self.entry = entry
        self._coordinator = coordinator
        self._cached_value = None
        self._attr_unique_id = f"{entry.entry_id}_ml_production_time"
        self._attr_translation_key = "production_time"
        self._attr_icon = "mdi:timer-outline"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Sensor availability. @zara"""
        return self._cached_value is not None

    @property
    def native_value(self) -> Optional[str]:
        """Return cached value. @zara"""
        return self._cached_value

    async def _load_from_db(self) -> None:
        """Load production time from coordinator data. @zara"""
        try:
            if self._coordinator.data:
                production_time = self._coordinator.data.get(
                    DATA_KEY_PRODUCTION_TIME, {}
                )
                duration_seconds = production_time.get(PROD_TIME_DURATION_SECONDS, 0)
                hours, remaining_seconds = divmod(duration_seconds, 3600)
                minutes, seconds = divmod(remaining_seconds, 60)
                self._cached_value = (
                    f"{int(hours):02}:{int(minutes):02}:{int(seconds):02}"
                )
            else:
                self._cached_value = "00:00:00"
        except Exception as error:
            _LOGGER.warning(f"Failed to load ProductionTimeSensor: {error}")
            self._cached_value = None
        return None if True else None

    async def async_added_to_hass(self) -> None:
        """Setup sensor. @zara"""
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_db()
        self.async_write_ha_state()


class MaxPeakTodaySensor(SensorEntity):
    """Sensor for today's maximum power peak. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the max peak today sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._attr_unique_id = f"{entry.entry_id}_ml_max_peak_today"
        self._attr_translation_key = "max_peak_today"
        self._attr_native_unit_of_measurement = "W"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_icon = "mdi:lightning-bolt"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Always available. @zara"""
        return True

    @property
    def native_value(self) -> float:
        """Return max peak or 0 if no data. @zara"""
        return self._cached_value if self._cached_value is not None else 0.0

    async def _load_from_db(self) -> None:
        """Load max peak today from coordinator data. @zara"""
        try:
            if self._coordinator.data:
                peak_today = self._coordinator.data.get(DATA_KEY_PEAK_TODAY, {})
                self._cached_value = peak_today.get(PEAK_TODAY_POWER_W, 0.0)
            else:
                self._cached_value = 0.0
        except Exception as error:
            _LOGGER.warning(f"Failed to load MaxPeakTodaySensor: {error}")
            self._cached_value = 0.0
        return None if True else None

    async def async_added_to_hass(self) -> None:
        """Setup sensor. @zara"""
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_db()
        self.async_write_ha_state()


class MaxPeakAllTimeSensor(SensorEntity):
    """Sensor for all-time maximum power peak. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the max peak all time sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._cached_date = None
        self._attr_unique_id = f"{entry.entry_id}_ml_max_peak_all_time"
        self._attr_translation_key = "max_peak_all_time"
        self._attr_native_unit_of_measurement = "W"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_icon = "mdi:lightning-bolt-circle"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Always available. @zara"""
        return True

    @property
    def native_value(self) -> float:
        """Return max peak all time or 0 if no data. @zara"""
        return self._cached_value if self._cached_value is not None else 0.0

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes. @zara"""
        if self._cached_date:
            return {"date": self._cached_date}
        return {}

    async def _load_from_db(self) -> None:
        """Load all-time peak from coordinator data. @zara"""
        try:
            if self._coordinator.data:
                statistics = self._coordinator.data.get(DATA_KEY_STATISTICS, {})
                all_time_peak = statistics.get(STATS_ALL_TIME_PEAK, {})
                self._cached_value = all_time_peak.get(PEAK_TODAY_POWER_W, 0.0)
                self._cached_date = all_time_peak.get("date")
            else:
                self._cached_value = 0.0
        except Exception as error:
            _LOGGER.warning(f"Failed to load MaxPeakAllTimeSensor: {error}")
            self._cached_value = 0.0
        return None if True else None

    async def async_added_to_hass(self) -> None:
        """Setup sensor. @zara"""
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_db()
        self.async_write_ha_state()


class ForecastDayAfterTomorrowSensor(SensorEntity):
    """Sensor for day after tomorrow's solar forecast. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the day after tomorrow sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._attr_unique_id = f"{entry.entry_id}_ml_forecast_day_after_tomorrow"
        self._attr_translation_key = "forecast_day_after_tomorrow"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:calendar-arrow-right"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Sensor availability. @zara"""
        return self._cached_value is not None

    @property
    def native_value(self) -> Optional[float]:
        """Return cached value. @zara"""
        return self._cached_value

    @property
    def extra_state_attributes(self) -> dict:
        """Return hourly forecast breakdown for day after tomorrow. @zara"""
        hourly_prediction_cache = getattr(
            self._coordinator, CACHE_HOURLY_PREDICTIONS, None
        )
        if not hourly_prediction_cache:
            return {}
        return _build_hourly_attributes(
            hourly_prediction_cache.get(CACHE_PREDICTIONS_DAY_AFTER, [])
        )

    async def _load_from_db(self) -> None:
        """Load day after tomorrow forecast directly from daily_forecasts DB table. @zara"""
        try:
            db_manager = self._coordinator.data_manager._db_manager
            if not db_manager:
                return
            from homeassistant.util import dt as dt_util

            day_after_str = (dt_util.now() + timedelta(days=2)).date().isoformat()
            day_after_forecast_row = await db_manager.fetchone(
                "SELECT df.prediction_kwh, COUNT(DISTINCT hp.target_hour)\n                   FROM daily_forecasts df\n                   LEFT JOIN hourly_predictions hp\n                     ON hp.target_date = df.forecast_date\n                   WHERE df.forecast_type = 'day_after_tomorrow'\n                     AND df.forecast_date = ?\n                   GROUP BY df.prediction_kwh",
                (day_after_str,),
            )
            if day_after_forecast_row and day_after_forecast_row[0] is not None:
                if (
                    int(day_after_forecast_row[1] or 0)
                    >= MIN_PREDICTION_HOURS_TO_LOCK_DAY
                ):
                    self._cached_value = round(float(day_after_forecast_row[0]), 2)
                    return
            hourly_prediction_cache = (
                getattr(self._coordinator, CACHE_HOURLY_PREDICTIONS, {}) or {}
            )
            day_after_predictions = hourly_prediction_cache.get(
                CACHE_PREDICTIONS_DAY_AFTER, []
            )
            if self._coordinator.data and hourly_forecast_date_covered(
                day_after_predictions, day_after_str
            ):
                coordinator_day_after_forecast = self._coordinator.data.get(
                    DATA_KEY_FORECAST_DAY_AFTER
                )
                if isinstance(coordinator_day_after_forecast, (int, float)):
                    self._cached_value = float(coordinator_day_after_forecast)
                elif isinstance(coordinator_day_after_forecast, dict):
                    self._cached_value = coordinator_day_after_forecast.get(
                        PRED_PREDICTION_KWH
                    )
                if True:
                    return
            self._cached_value = None
            _LOGGER.debug(
                "Day-after forecast for %s not yet available (hourly weather horizon incomplete)",
                day_after_str,
            )
        except Exception as error:
            _LOGGER.warning(
                "Failed to load ForecastDayAfterTomorrowSensor from DB: %s", error
            )
            self._cached_value = None
        if True:
            return

    async def async_added_to_hass(self) -> None:
        """Setup sensor. @zara"""
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_db()
        self.async_write_ha_state()


class MonthlyYieldSensor(SensorEntity):
    """Sensor for current month's total yield. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the monthly yield sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = 0.0
        self._attr_unique_id = f"{entry.entry_id}_ml_monthly_yield"
        self._attr_translation_key = "monthly_yield"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:calendar-month"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Always available, shows 0.0 if no data. @zara"""
        return True

    @property
    def native_value(self) -> float:
        """Return value or 0.0 if None. @zara"""
        return self._cached_value

    async def _load_from_db(self) -> None:
        """Load monthly yield from coordinator. @zara"""
        try:
            if self._coordinator.data:
                statistics = self._coordinator.data.get(DATA_KEY_STATISTICS, {})
                month_statistics = statistics.get(STATS_CURRENT_MONTH, {})
                self._cached_value = month_statistics.get(STATS_YIELD_KWH, 0.0)
        except Exception as error:
            _LOGGER.warning(f"Failed to load MonthlyYieldSensor: {error}")
        return None if True else None

    async def async_added_to_hass(self) -> None:
        """Setup sensor. @zara"""
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_db()
        self.async_write_ha_state()


class MonthlyConsumptionSensor(SensorEntity):
    """Sensor for current month's total consumption. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the monthly consumption sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = 0.0
        self._attr_unique_id = f"{entry.entry_id}_ml_monthly_consumption"
        self._attr_translation_key = "monthly_consumption"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:home-lightning-bolt"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Always available, shows 0.0 if no data. @zara"""
        return True

    @property
    def native_value(self) -> float:
        """Return value or 0.0 if None. @zara"""
        return self._cached_value

    async def _load_from_db(self) -> None:
        """Load monthly consumption from coordinator. @zara"""
        try:
            if self._coordinator.data:
                statistics = self._coordinator.data.get(DATA_KEY_STATISTICS, {})
                month_statistics = statistics.get(STATS_CURRENT_MONTH, {})
                self._cached_value = month_statistics.get(STATS_CONSUMPTION_KWH, 0.0)
        except Exception as error:
            _LOGGER.warning(f"Failed to load MonthlyConsumptionSensor: {error}")
        return None if True else None

    async def async_added_to_hass(self) -> None:
        """Setup sensor. @zara"""
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_db()
        self.async_write_ha_state()


class WeeklyYieldSensor(SensorEntity):
    """Sensor for current week's total yield. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the weekly yield sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = 0.0
        self._attr_unique_id = f"{entry.entry_id}_ml_weekly_yield"
        self._attr_translation_key = "weekly_yield"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:calendar-week"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Always available, shows 0.0 if no data. @zara"""
        return True

    @property
    def native_value(self) -> float:
        """Return value or 0.0 if None. @zara"""
        return self._cached_value

    async def _load_from_db(self) -> None:
        """Load weekly yield from coordinator. @zara"""
        try:
            if self._coordinator.data:
                statistics = self._coordinator.data.get(DATA_KEY_STATISTICS, {})
                week_statistics = statistics.get(STATS_CURRENT_WEEK, {})
                self._cached_value = week_statistics.get(STATS_YIELD_KWH, 0.0)
        except Exception as error:
            _LOGGER.warning(f"Failed to load WeeklyYieldSensor: {error}")
        return None if True else None

    async def async_added_to_hass(self) -> None:
        """Setup sensor. @zara"""
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_db()
        self.async_write_ha_state()


class WeeklyConsumptionSensor(SensorEntity):
    """Sensor for current week's total consumption. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the weekly consumption sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = 0.0
        self._attr_unique_id = f"{entry.entry_id}_ml_weekly_consumption"
        self._attr_translation_key = "weekly_consumption"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:home-lightning-bolt-outline"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Always available, shows 0.0 if no data. @zara"""
        return True

    @property
    def native_value(self) -> float:
        """Return value or 0.0 if None. @zara"""
        return self._cached_value

    async def _load_from_db(self) -> None:
        """Load weekly consumption from coordinator. @zara"""
        try:
            if self._coordinator.data:
                statistics = self._coordinator.data.get(DATA_KEY_STATISTICS, {})
                week_statistics = statistics.get(STATS_CURRENT_WEEK, {})
                self._cached_value = week_statistics.get(STATS_CONSUMPTION_KWH, 0.0)
        except Exception as error:
            _LOGGER.warning(f"Failed to load WeeklyConsumptionSensor: {error}")
        return None if True else None

    async def async_added_to_hass(self) -> None:
        """Setup sensor. @zara"""
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_db()
        self.async_write_ha_state()


class AverageYield7DaysSensor(SensorEntity):
    """Sensor for average daily yield over last 7 days. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the average yield 7 days sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._attr_unique_id = f"{entry.entry_id}_ml_avg_yield_7d"
        self._attr_translation_key = "avg_yield_7d"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:chart-line"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Sensor availability. @zara"""
        return self._cached_value is not None

    @property
    def native_value(self) -> Optional[float]:
        """Return cached value. @zara"""
        return self._cached_value

    @property
    def extra_state_attributes(self) -> dict:
        """Return transparency metrics for the last 7-day window. @zara"""
        statistics = (
            self._coordinator.data.get(DATA_KEY_STATISTICS, {})
            if self._coordinator.data
            else {}
        )
        last_7_days_statistics = statistics.get(STATS_LAST_7_DAYS, {})
        return {
            "avg_accuracy_percent": last_7_days_statistics.get(STATS_AVG_ACCURACY),
            "evaluation_coverage_percent": last_7_days_statistics.get(
                "avg_evaluation_coverage_percent"
            ),
            "avg_excluded_mppt_hours": last_7_days_statistics.get(
                "avg_excluded_mppt_hours"
            ),
            "total_yield_kwh": last_7_days_statistics.get("total_yield_kwh"),
            "calculated_at": last_7_days_statistics.get("calculated_at"),
        }

    async def _load_from_db(self) -> None:
        """Load average yield 7 days from coordinator. @zara"""
        try:
            if self._coordinator.data:
                statistics = self._coordinator.data.get(DATA_KEY_STATISTICS, {})
                last_7_days_statistics = statistics.get(STATS_LAST_7_DAYS, {})
                self._cached_value = last_7_days_statistics.get(STATS_AVG_YIELD_KWH)
        except Exception as error:
            _LOGGER.warning(f"Failed to load AverageYield7DaysSensor: {error}")
        return None if True else None

    async def async_added_to_hass(self) -> None:
        """Setup sensor. @zara"""
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_db()
        self.async_write_ha_state()


class AverageYield30DaysSensor(SensorEntity):
    """Sensor for average daily yield over last 30 days. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the average yield 30 days sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._attr_unique_id = f"{entry.entry_id}_ml_avg_yield_30d"
        self._attr_translation_key = "avg_yield_30d"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:chart-bar"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Sensor availability. @zara"""
        return self._cached_value is not None

    @property
    def native_value(self) -> Optional[float]:
        """Return cached value. @zara"""
        return self._cached_value

    @property
    def extra_state_attributes(self) -> dict:
        """Return transparency metrics for the last 30-day window. @zara"""
        statistics = (
            self._coordinator.data.get(DATA_KEY_STATISTICS, {})
            if self._coordinator.data
            else {}
        )
        last_30_days_statistics = statistics.get(STATS_LAST_30_DAYS, {})
        return {
            "avg_accuracy_percent": last_30_days_statistics.get(STATS_AVG_ACCURACY),
            "evaluation_coverage_percent": last_30_days_statistics.get(
                "avg_evaluation_coverage_percent"
            ),
            "avg_excluded_mppt_hours": last_30_days_statistics.get(
                "avg_excluded_mppt_hours"
            ),
            "total_yield_kwh": last_30_days_statistics.get("total_yield_kwh"),
            "calculated_at": last_30_days_statistics.get("calculated_at"),
        }

    async def _load_from_db(self) -> None:
        """Load average yield 30 days from coordinator. @zara"""
        try:
            if self._coordinator.data:
                statistics = self._coordinator.data.get(DATA_KEY_STATISTICS, {})
                last_30_days_statistics = statistics.get(STATS_LAST_30_DAYS, {})
                self._cached_value = last_30_days_statistics.get(STATS_AVG_YIELD_KWH)
        except Exception as error:
            _LOGGER.warning(f"Failed to load AverageYield30DaysSensor: {error}")
        return None if True else None

    async def async_added_to_hass(self) -> None:
        """Setup sensor. @zara"""
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_db()
        self.async_write_ha_state()


class AverageAccuracy30DaysSensor(SensorEntity):
    """Sensor for average accuracy over last 30 days. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the average accuracy 30 days sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._attr_unique_id = f"{entry.entry_id}_ml_avg_accuracy_30d"
        self._attr_translation_key = "avg_accuracy_30d"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:target"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Sensor availability. @zara"""
        return self._cached_value is not None

    @property
    def native_value(self) -> Optional[float]:
        """Return cached value. @zara"""
        return self._cached_value

    @property
    def extra_state_attributes(self) -> dict:
        """Return clean-evaluation transparency for the 30-day accuracy window. @zara"""
        statistics = (
            self._coordinator.data.get(DATA_KEY_STATISTICS, {})
            if self._coordinator.data
            else {}
        )
        last_30_days_statistics = statistics.get(STATS_LAST_30_DAYS, {})
        last_7_days_statistics = statistics.get(STATS_LAST_7_DAYS, {})
        return {
            "evaluation_coverage_percent_30d": last_30_days_statistics.get(
                "avg_evaluation_coverage_percent"
            ),
            "avg_excluded_mppt_hours_30d": last_30_days_statistics.get(
                "avg_excluded_mppt_hours"
            ),
            "avg_yield_kwh_30d": last_30_days_statistics.get(STATS_AVG_YIELD_KWH),
            "evaluation_coverage_percent_7d": last_7_days_statistics.get(
                "avg_evaluation_coverage_percent"
            ),
            "avg_excluded_mppt_hours_7d": last_7_days_statistics.get(
                "avg_excluded_mppt_hours"
            ),
            "avg_yield_kwh_7d": last_7_days_statistics.get(STATS_AVG_YIELD_KWH),
            "calculated_at": last_30_days_statistics.get("calculated_at"),
        }

    async def _load_from_db(self) -> None:
        """Load average accuracy 30 days from coordinator. @zara"""
        try:
            if self._coordinator.data:
                statistics = self._coordinator.data.get(DATA_KEY_STATISTICS, {})
                last_30_days_statistics = statistics.get(STATS_LAST_30_DAYS, {})
                self._cached_value = last_30_days_statistics.get(STATS_AVG_ACCURACY)
        except Exception as error:
            _LOGGER.warning(f"Failed to load AverageAccuracy30DaysSensor: {error}")
        return None if True else None

    async def async_added_to_hass(self) -> None:
        """Setup sensor. @zara"""
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        """Reload value and update state. @zara"""
        await self._load_from_db()
        self.async_write_ha_state()


class EvaluationCoverage7DaysSensor(SensorEntity):
    """Sensor for average forecast evaluation coverage over last 7 days. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._attr_unique_id = f"{entry.entry_id}_ml_evaluation_coverage_7d"
        self._attr_translation_key = "evaluation_coverage_7d"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:chart-donut"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        return self._cached_value is not None

    @property
    def native_value(self) -> Optional[float]:
        return self._cached_value

    async def _load_from_db(self) -> None:
        try:
            if self._coordinator.data:
                statistics = self._coordinator.data.get(DATA_KEY_STATISTICS, {})
                last_7_days_statistics = statistics.get(STATS_LAST_7_DAYS, {})
                self._cached_value = last_7_days_statistics.get(
                    "avg_evaluation_coverage_percent"
                )
        except Exception as error:
            _LOGGER.warning(f"Failed to load EvaluationCoverage7DaysSensor: {error}")
        return None if True else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        await self._load_from_db()
        self.async_write_ha_state()


class EvaluationCoverage30DaysSensor(SensorEntity):
    """Sensor for average forecast evaluation coverage over last 30 days. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._attr_unique_id = f"{entry.entry_id}_ml_evaluation_coverage_30d"
        self._attr_translation_key = "evaluation_coverage_30d"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:chart-arc"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        return self._cached_value is not None

    @property
    def native_value(self) -> Optional[float]:
        return self._cached_value

    async def _load_from_db(self) -> None:
        try:
            if self._coordinator.data:
                statistics = self._coordinator.data.get(DATA_KEY_STATISTICS, {})
                last_30_days_statistics = statistics.get(STATS_LAST_30_DAYS, {})
                self._cached_value = last_30_days_statistics.get(
                    "avg_evaluation_coverage_percent"
                )
        except Exception as error:
            _LOGGER.warning(f"Failed to load EvaluationCoverage30DaysSensor: {error}")
        return None if True else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        await self._load_from_db()
        self.async_write_ha_state()


class ExcludedMpptHours7DaysSensor(SensorEntity):
    """Sensor for average excluded MPPT hours over last 7 days. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._attr_unique_id = f"{entry.entry_id}_ml_excluded_mppt_hours_7d"
        self._attr_translation_key = "excluded_mppt_hours_7d"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:transmission-tower-off"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        return self._cached_value is not None

    @property
    def native_value(self) -> Optional[float]:
        return self._cached_value

    async def _load_from_db(self) -> None:
        try:
            if self._coordinator.data:
                statistics = self._coordinator.data.get(DATA_KEY_STATISTICS, {})
                last_7_days_statistics = statistics.get(STATS_LAST_7_DAYS, {})
                self._cached_value = last_7_days_statistics.get(
                    "avg_excluded_mppt_hours"
                )
        except Exception as error:
            _LOGGER.warning(f"Failed to load ExcludedMpptHours7DaysSensor: {error}")
        return None if True else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        await self._load_from_db()
        self.async_write_ha_state()


class ExcludedMpptHours30DaysSensor(SensorEntity):
    """Sensor for average excluded MPPT hours over last 30 days. @zara"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        self._coordinator = coordinator
        self.entry = entry
        self._cached_value = None
        self._attr_unique_id = f"{entry.entry_id}_ml_excluded_mppt_hours_30d"
        self._attr_translation_key = "excluded_mppt_hours_30d"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:transmission-tower-export"
        self._attr_suggested_display_precision = 2
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        return self._cached_value is not None

    @property
    def native_value(self) -> Optional[float]:
        return self._cached_value

    async def _load_from_db(self) -> None:
        try:
            if self._coordinator.data:
                statistics = self._coordinator.data.get(DATA_KEY_STATISTICS, {})
                last_30_days_statistics = statistics.get(STATS_LAST_30_DAYS, {})
                self._cached_value = last_30_days_statistics.get(
                    "avg_excluded_mppt_hours"
                )
        except Exception as error:
            _LOGGER.warning(f"Failed to load ExcludedMpptHours30DaysSensor: {error}")
        return None if True else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._load_from_db()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._reload_and_update())

    async def _reload_and_update(self) -> None:
        await self._load_from_db()
        self.async_write_ha_state()


class EvccForecastSensor(SensorEntity):
    """Sensor providing solar forecast data in evcc-compatible JSON format. @zara

    Outputs hourly forecast data as a JSON array in extra_state_attributes['forecast']
    with the format evcc expects: [{"start": "...", "end": "...", "value": <watts>}, ...]
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SolarForecastMLCoordinator, entry: ConfigEntry):
        """Initialize the evcc forecast sensor. @zara"""
        self._coordinator = coordinator
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ml_evcc_forecast"
        self._attr_translation_key = "evcc_forecast"
        self._attr_icon = "mdi:ev-station"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        """Sensor availability. @zara"""
        return True

    @property
    def native_value(self) -> str:
        """Return number of forecast slots as state. @zara"""
        forecast_entries = self._build_evcc_forecast()
        return f"{len(forecast_entries)} slots"

    @property
    def extra_state_attributes(self) -> dict:
        """Return evcc-compatible forecast JSON array. @zara"""
        return {"forecast": self._build_evcc_forecast()}

    def _build_evcc_forecast(self) -> list:
        """Build forecast array in evcc format from SFML hourly predictions. @zara

        evcc expects: [{"start": "2026-03-07T10:00:00", "end": "2026-03-07T11:00:00", "value": 1250.5}, ...]
        value = average power in Watts for the time slot (kWh * 1000 for a 1-hour slot)
        """
        hourly_prediction_cache = getattr(
            self._coordinator, CACHE_HOURLY_PREDICTIONS, None
        )
        if not hourly_prediction_cache:
            return []
        forecast_entries = []
        for prediction in hourly_prediction_cache.get(CACHE_PREDICTIONS, []):
            forecast_entry = self._pred_to_evcc_entry(prediction)
            if forecast_entry:
                forecast_entries.append(forecast_entry)
        for prediction in hourly_prediction_cache.get(CACHE_PREDICTIONS_TOMORROW, []):
            forecast_entry = self._pred_to_evcc_entry(prediction)
            if forecast_entry:
                forecast_entries.append(forecast_entry)
        for prediction in hourly_prediction_cache.get(CACHE_PREDICTIONS_DAY_AFTER, []):
            forecast_entry = self._pred_to_evcc_entry(prediction)
            if forecast_entry:
                forecast_entries.append(forecast_entry)
        forecast_entries.sort(key=lambda x: x["start"])
        return forecast_entries

    @staticmethod
    def _pred_to_evcc_entry(pred: dict) -> Optional[dict]:
        """Convert a single SFML prediction to evcc format. @zara"""
        target_date = pred.get(PRED_TARGET_DATE)
        target_hour = pred.get(PRED_TARGET_HOUR)
        prediction_kwh = pred.get(PRED_PREDICTION_KWH) or pred.get(
            PRED_PREDICTED_KWH, 0.0
        )
        if target_date is None or target_hour is None or prediction_kwh is None:
            return None
        average_power_w = round(float(prediction_kwh) * 1000, 1)
        slot_start = f"{target_date}T{target_hour:02d}:00:00"
        next_hour = target_hour + 1
        if next_hour >= 24:
            return
        slot_end = f"{target_date}T{next_hour:02d}:00:00"
        return {"start": slot_start, "end": slot_end, "value": average_power_w}

    async def async_added_to_hass(self) -> None:
        """Setup sensor. @zara"""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates. @zara"""
        self.async_write_ha_state()
