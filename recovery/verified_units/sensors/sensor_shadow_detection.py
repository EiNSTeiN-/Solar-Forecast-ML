# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Subspace anomaly detection sensors for Warp Core Simulation.
Detects gravitational lensing artifacts and spatial distortions affecting
cochrane field readings. All sensors read from controller cache - no direct file I/O.
"""
import logging
import math
from typing import Any, Dict, List, Optional
from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from ..const import (
    DOMAIN,
    INTEGRATION_MODEL,
    SOFTWARE_VERSION,
    AI_VERSION,
    CACHE_HOURLY_PREDICTIONS,
    CACHE_PREDICTIONS,
    PRED_TARGET_DATE,
)

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
    dev_mode_data = integration_data.get("dev_mode")
    if not isinstance(dev_mode_data, dict):
        return False
    return bool(dev_mode_data.get("active"))


_ROOT_CAUSE_TRANSLATIONS = {
    "de": {
        "night": "Nacht",
        "low_sun_angle": "Flacher Sonnenwinkel",
        "low_radiation": "Geringe Einstrahlung",
        "weather_clouds": "Wolken",
        "building_tree_obstruction": "Gebäude-/Baumschatten",
        "possible_obstruction": "Mögliche Verschattung",
        "normal_variation": "Normale Schwankung",
        "clearer_than_forecast": "Klarer als Prognose",
        "weather_better_than_forecast": "Wetter besser als Prognose",
        "panel_frost": "Frost auf Modulen",
        "snow_frost": "Schnee auf Modulen",
        "unknown": "Unbekannt",
    },
    "fr": {
        "night": "Nuit",
        "low_sun_angle": "Angle solaire bas",
        "low_radiation": "Faible rayonnement",
        "weather_clouds": "Nuages",
        "building_tree_obstruction": "Ombre bâtiment/arbre",
        "possible_obstruction": "Ombrage possible",
        "normal_variation": "Variation normale",
        "clearer_than_forecast": "Plus clair que prévu",
        "weather_better_than_forecast": "Météo meilleure que prévue",
        "panel_frost": "Givre sur panneaux",
        "snow_frost": "Neige sur panneaux",
        "unknown": "Inconnu",
    },
    "es": {
        "night": "Noche",
        "low_sun_angle": "Ángulo solar bajo",
        "low_radiation": "Radiación baja",
        "weather_clouds": "Nubes",
        "building_tree_obstruction": "Sombra edificio/árbol",
        "possible_obstruction": "Posible sombra",
        "normal_variation": "Variación normal",
        "clearer_than_forecast": "Más claro que pronóstico",
        "weather_better_than_forecast": "Clima mejor que pronóstico",
        "panel_frost": "Escarcha en paneles",
        "snow_frost": "Nieve en paneles",
        "unknown": "Desconocido",
    },
    "ru": {
        "night": "Ночь",
        "low_sun_angle": "Низкий угол солнца",
        "low_radiation": "Слабое излучение",
        "weather_clouds": "Облака",
        "building_tree_obstruction": "Тень здания/дерева",
        "possible_obstruction": "Возможное затенение",
        "normal_variation": "Нормальное отклонение",
        "clearer_than_forecast": "Яснее прогноза",
        "weather_better_than_forecast": "Погода лучше прогноза",
        "panel_frost": "Иней на панелях",
        "snow_frost": "Снег на панелях",
        "unknown": "Неизвестно",
    },
}
_ROOT_CAUSE_DISPLAY_EN = {
    "night": "Night",
    "low_sun_angle": "Low Sun Angle",
    "low_radiation": "Low Radiation",
    "weather_clouds": "Clouds",
    "building_tree_obstruction": "Building/Tree Shadow",
    "possible_obstruction": "Possible Obstruction",
    "normal_variation": "Normal Variation",
    "clearer_than_forecast": "Clearer than Forecast",
    "weather_better_than_forecast": "Weather better than Forecast",
    "panel_frost": "Panel Frost",
    "snow_frost": "Snow on Panels",
    "unknown": "Unknown",
}


def _translate_root_cause(root_cause: str, hass=None) -> str:
    """Translate root_cause to user's HA language. Falls back to English display name. @zara"""
    language = None
    if hass:
        try:
            language = hass.config.language
        except Exception:
            pass
    if language and language in _ROOT_CAUSE_TRANSLATIONS:
        return _ROOT_CAUSE_TRANSLATIONS[language].get(
            root_cause, _ROOT_CAUSE_DISPLAY_EN.get(root_cause, root_cause)
        )
    return _ROOT_CAUSE_DISPLAY_EN.get(root_cause, root_cause)


def _shadow_data(prediction: Dict[str, Any]) -> Dict[str, Any]:
    shadow = prediction.get("shadow_detection")
    return shadow if isinstance(shadow, dict) else {}


def _numeric_value(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric_value = float(value)
    return numeric_value if math.isfinite(numeric_value) else None


def _safe_round(value: Any, digits: int) -> Optional[float]:
    numeric_value = _numeric_value(value)
    return round(numeric_value, digits) if numeric_value is not None else None


def _root_cause_key(value: Any) -> str:
    return value if isinstance(value, str) and value else "unknown"


def _shadow_type_key(value: Any) -> str:
    return value if isinstance(value, str) and value else "unknown"


def _cause_breakdown(
    root_cause: str, shadow_percent: Optional[float]
) -> Dict[str, Optional[float]]:
    breakdown = {
        "weather_clouds": 0.0,
        "building_tree_obstruction": 0.0,
        "low_sun_angle": 0.0,
    }
    if root_cause in breakdown:
        breakdown[root_cause] = shadow_percent
    return breakdown


def _get_today_predictions_from_cache(coordinator) -> List[Dict[str, Any]]:
    """Get today's predictions from coordinator cache - NO FILE I/O. @zara"""
    try:
        if not coordinator:
            return []
        prediction_cache = getattr(coordinator, CACHE_HOURLY_PREDICTIONS, None)
        if not prediction_cache:
            _LOGGER.debug("No hourly predictions cache available in coordinator")
            return []
        today = dt_util.now().date().isoformat()
        return [
            prediction
            for prediction in prediction_cache.get(CACHE_PREDICTIONS, [])
            if prediction.get(PRED_TARGET_DATE) == today
        ]
    except Exception as error:
        _LOGGER.debug(f"Error getting today predictions from cache: {error}")
        return []


def _filter_valid_shadow_predictions(predictions: List[Dict]) -> List[Dict]:
    """Filter predictions to daylight hours. @zara V16.1

    Changed: No longer requires shadow_detection data.
    Now filters based on production hours (6-20) to show snow/frost even without shadow data.
    """
    return [
        prediction
        for prediction in predictions
        if prediction.get("target_hour") is not None
        if 6 <= prediction.get("target_hour", -1) <= 20
    ]


class ShadowCurrentSensor(CoordinatorEntity, SensorEntity):
    """Sensor for current hour shadow detection status. @zara"""

    def __init__(self, coordinator, entry):
        """Initialize the sensor. @zara"""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_has_entity_name = True
        self._attr_translation_key = "shadow_current"
        self._attr_unique_id = f"{entry.entry_id}_shadow_current"
        self._attr_device_class = None
        self._attr_state_class = None
        self._attr_icon = "mdi:weather-sunny-alert"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Solar Forecast ML",
            manufacturer="Zara-Toorox",
            model=INTEGRATION_MODEL,
            sw_version=f"SW {SOFTWARE_VERSION} | AI {AI_VERSION}",
            configuration_url="https://github.com/Zara-Toorox/ha-solar-forecast-ml",
        )
        self._cached_prediction = None
        self._cache_hour = None

    def _get_current_prediction(self) -> Optional[Dict[str, Any]]:
        """Get current hour prediction with caching. @zara"""
        now = dt_util.now()
        current_hour = now.hour
        if self._cache_hour == current_hour and self._cached_prediction is not None:
            return self._cached_prediction
        try:
            date_str = now.date().isoformat()
            prediction_id = f"{date_str}_{current_hour:02d}"
            prediction_cache = getattr(self.coordinator, CACHE_HOURLY_PREDICTIONS, None)
            if prediction_cache and prediction_cache.get(CACHE_PREDICTIONS):
                self._cached_prediction = next(
                    (
                        prediction
                        for prediction in prediction_cache[CACHE_PREDICTIONS]
                        if prediction.get("prediction_id") == prediction_id
                    ),
                    None,
                )
            else:
                self._cached_prediction = None
            self._cache_hour = current_hour
            return self._cached_prediction
        except Exception as error:
            _LOGGER.debug(f"Error refreshing prediction cache: {error}")
            self._cached_prediction = None
            self._cache_hour = current_hour
            return

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates - invalidate cache. @zara"""
        self._cache_hour = None
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Return if entity is available. @zara"""
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> Optional[str]:
        """Return the current shadow status. @zara"""
        prediction = self._get_current_prediction()
        if not prediction:
            return "no_data"
        shadow = _shadow_data(prediction)
        shadow_type = shadow.get("shadow_type", "unknown")
        status_by_shadow_type = {
            "none": "clear",
            "light": "light_shadow",
            "moderate": "moderate_shadow",
            "heavy": "heavy_shadow",
            "night": "night",
            "error": "error",
            "unknown": "no_data",
        }
        return status_by_shadow_type.get(shadow_type, "no_data")

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional shadow detection attributes. @zara"""
        if not _published_dev_mode_active(self):
            return {}
        prediction = self._get_current_prediction()
        if not prediction:
            return {"status": "no_prediction_data"}
        try:
            shadow = _shadow_data(prediction)
            attributes = {
                "shadow_type": shadow.get("shadow_type", "unknown"),
                "shadow_percent": shadow.get("shadow_percent", 0),
                "confidence": shadow.get("confidence", 0),
                "root_cause": _translate_root_cause(
                    shadow.get("root_cause", "unknown"), self.hass
                ),
                "interpretation": shadow.get("interpretation", "N/A"),
                "efficiency_ratio": shadow.get("efficiency_ratio", 0),
                "loss_kwh": shadow.get("loss_kwh", 0),
            }
            theory_result = {}
            methods = shadow.get("methods", {})
            if isinstance(methods, dict) and methods:
                theory_result = methods.get("theory_ratio", {})
                fusion_result = methods.get("sensor_fusion", {})
                if not isinstance(theory_result, dict):
                    theory_result = {}
                if not isinstance(fusion_result, dict):
                    fusion_result = {}
                attributes["method_theory_shadow"] = theory_result.get(
                    "shadow_percent", 0
                )
                attributes["method_theory_confidence"] = theory_result.get(
                    "confidence", 0
                )
                attributes["method_fusion_shadow"] = fusion_result.get(
                    "shadow_percent", 0
                )
                attributes["method_fusion_confidence"] = fusion_result.get(
                    "confidence", 0
                )
                attributes["method_fusion_mode"] = fusion_result.get("mode", "unknown")
            attributes["actual_kwh"] = prediction.get("actual_kwh", 0)
            attributes["theoretical_max_kwh"] = shadow.get("theoretical_max_kwh", 0)
            attributes["clear_sky_loss_percent"] = shadow.get(
                "clear_sky_loss_percent", theory_result.get("shadow_percent")
            )
            attributes["local_shadow_percent"] = shadow.get(
                "local_shadow_percent", shadow.get("shadow_percent", 0)
            )
            attributes["weather_loss_percent"] = shadow.get("weather_loss_percent")
            attributes["local_shadow_evaluable"] = shadow.get("evaluable", True)
            return attributes
        except Exception as error:
            _LOGGER.error(f"Error getting shadow current attributes: {error}")
            return {"error": str(error)}

    @property
    def icon(self) -> str:
        """Return icon based on shadow status. @zara"""
        prediction = self._get_current_prediction()
        if not prediction:
            return "mdi:help-circle"
        shadow = _shadow_data(prediction)
        shadow_type = shadow.get("shadow_type", "unknown")
        icons_by_shadow_type = {
            "none": "mdi:weather-sunny",
            "light": "mdi:weather-partly-cloudy",
            "moderate": "mdi:weather-cloudy",
            "heavy": "mdi:weather-cloudy-alert",
            "night": "mdi:weather-night",
            "error": "mdi:alert-circle",
        }
        return icons_by_shadow_type.get(shadow_type, "mdi:help-circle")


class ShadowTodaySensor(CoordinatorEntity, SensorEntity):
    """Sensor for today's cumulative shadow analysis. @zara"""

    def __init__(self, coordinator, entry):
        """Initialize the sensor. @zara"""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_has_entity_name = True
        self._attr_translation_key = "shadow_today"
        self._attr_unique_id = f"{entry.entry_id}_shadow_today"
        self._attr_device_class = None
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "hours"
        self._attr_icon = "mdi:weather-sunset"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Solar Forecast ML",
            manufacturer="Zara-Toorox",
            model=INTEGRATION_MODEL,
            sw_version=f"SW {SOFTWARE_VERSION} | AI {AI_VERSION}",
            configuration_url="https://github.com/Zara-Toorox/ha-solar-forecast-ml",
        )
        self._cached_analysis = None
        self._cache_date = None

    def _get_today_analysis(self) -> Dict[str, Any]:
        """Calculate today's shadow analysis with caching. @zara"""
        today = dt_util.now().date().isoformat()
        if self._cache_date == today and self._cached_analysis is not None:
            return self._cached_analysis
        try:
            today_predictions = _get_today_predictions_from_cache(self.coordinator)
            daylight_predictions = _filter_valid_shadow_predictions(today_predictions)
            if not daylight_predictions:
                self._cached_analysis = {
                    "status": "no_data",
                    "shadow_hours": 0,
                    "hourly_breakdown": [],
                }
                self._cache_date = today
                return self._cached_analysis
            shadow_type_counts = {"none": 0, "light": 0, "moderate": 0, "heavy": 0}
            cause_counts = {
                "weather_clouds": 0,
                "building_tree_obstruction": 0,
                "possible_obstruction": 0,
                "low_sun_angle": 0,
                "other": 0,
            }
            cause_hours = {
                "weather_clouds": [],
                "building_tree_obstruction": [],
                "possible_obstruction": [],
                "low_sun_angle": [],
                "other": [],
            }
            hourly_breakdown = []
            cumulative_loss_kwh = 0.0
            theoretical_max_kwh = 0.0
            shadow_hours = []
            peak_shadow_hour = None
            peak_shadow_percent = 0.0
            incomplete_hours = []
            analyzed_hour_count = 0
            forecast_shadow_applied_hours = []
            snow_hours = []
            for daylight_prediction in daylight_predictions:
                shadow = _shadow_data(daylight_prediction)
                actual_weather = daylight_prediction.get("weather_actual", {})
                if not isinstance(actual_weather, dict):
                    actual_weather = {}
                shadow_type = _shadow_type_key(shadow.get("shadow_type", "unknown"))
                root_cause = _root_cause_key(shadow.get("root_cause", "unknown"))
                hour = daylight_prediction.get("target_hour")
                shadow_percent = _numeric_value(shadow.get("shadow_percent"))
                has_shadow_percent = shadow_percent is not None
                snow_covered = actual_weather.get("snow_covered_panels", False)
                snow_source = actual_weather.get("snow_coverage_source", "none")
                frost_detected = actual_weather.get("frost_detected", False)
                frost_type = actual_weather.get("frost_type")
                if snow_covered and hour is not None:
                    snow_hours.append(hour)
                if hour is not None:
                    if not has_shadow_percent:
                        incomplete_hours.append(hour)
                    cause_breakdown = _cause_breakdown(root_cause, shadow_percent)
                    hourly_breakdown.append(
                        {
                            "hour": hour,
                            "shadow_percent": _safe_round(shadow_percent, 1),
                            "root_cause": _translate_root_cause(root_cause, self.hass),
                            "shadow_type": shadow_type,
                            "cloud_pct": cause_breakdown["weather_clouds"],
                            "shadow_pct": cause_breakdown["building_tree_obstruction"],
                            "sun_pct": cause_breakdown["low_sun_angle"],
                            "snow_covered": snow_covered,
                            "snow_source": snow_source if snow_covered else None,
                            "frost_detected": frost_detected,
                            "frost_type": frost_type if frost_detected else None,
                            "status": (
                                "complete" if has_shadow_percent else "incomplete"
                            ),
                        }
                    )
                if has_shadow_percent:
                    analyzed_hour_count += 1
                if has_shadow_percent and shadow_type in shadow_type_counts:
                    shadow_type_counts[shadow_type] += 1
                learned_pattern = shadow.get("learned_pattern") or {}
                if (
                    isinstance(learned_pattern, dict)
                    and learned_pattern.get("is_fixed_obstruction")
                    and (hour is not None)
                ):
                    forecast_shadow_applied_hours.append(hour)
                if (
                    has_shadow_percent
                    and shadow_type in ("moderate", "heavy")
                    and (hour is not None)
                ):
                    shadow_hours.append(hour)
                    if root_cause in cause_counts:
                        cause_counts[root_cause] += 1
                        cause_hours[root_cause].append(hour)
                    else:
                        cause_counts["other"] += 1
                        cause_hours["other"].append(hour)
                loss_kwh = shadow.get("loss_kwh")
                hourly_theoretical_max_kwh = shadow.get("theoretical_max_kwh")
                if isinstance(loss_kwh, (int, float)) and loss_kwh >= 0:
                    cumulative_loss_kwh += loss_kwh
                if (
                    isinstance(hourly_theoretical_max_kwh, (int, float))
                    and hourly_theoretical_max_kwh >= 0
                ):
                    theoretical_max_kwh += hourly_theoretical_max_kwh
                hourly_shadow_percent = _numeric_value(shadow.get("shadow_percent"))
                if isinstance(hourly_shadow_percent, (int, float)):
                    if hourly_shadow_percent > peak_shadow_percent:
                        peak_shadow_percent = hourly_shadow_percent
                        peak_shadow_hour = daylight_prediction.get("target_hour")
                        continue
            if theoretical_max_kwh > 0:
                daily_loss_percent = cumulative_loss_kwh / theoretical_max_kwh * 100.0
            else:
                daily_loss_percent = 0.0
            self._cached_analysis = {
                "shadow_hours": float(len(shadow_hours)),
                "shadow_hours_count": len(shadow_hours),
                "shadow_hours_list": shadow_hours,
                "none_count": shadow_type_counts["none"],
                "light_count": shadow_type_counts["light"],
                "moderate_count": shadow_type_counts["moderate"],
                "heavy_count": shadow_type_counts["heavy"],
                "total_analyzed_hours": analyzed_hour_count,
                "total_candidate_hours": len(daylight_predictions),
                "incomplete_hours_count": len(incomplete_hours),
                "incomplete_hours_list": sorted(incomplete_hours),
                "peak_shadow_hour": peak_shadow_hour,
                "peak_shadow_percent": round(peak_shadow_percent, 1),
                "cumulative_loss_kwh": round(cumulative_loss_kwh, 3),
                "daily_loss_percent": round(daily_loss_percent, 1),
                "date": today,
                "cloud_hours": cause_counts["weather_clouds"],
                "cloud_hours_list": cause_hours["weather_clouds"],
                "real_shadow_hours": cause_counts["building_tree_obstruction"],
                "real_shadow_hours_list": cause_hours["building_tree_obstruction"],
                "candidate_hours": cause_counts["possible_obstruction"],
                "candidate_hours_list": cause_hours["possible_obstruction"],
                "forecast_shadow_applied": sorted(set(forecast_shadow_applied_hours)),
                "low_sun_hours": cause_counts["low_sun_angle"],
                "low_sun_hours_list": cause_hours["low_sun_angle"],
                "snow_hours": len(snow_hours),
                "snow_hours_list": snow_hours,
                "hourly_breakdown": sorted(hourly_breakdown, key=lambda x: x["hour"]),
            }
            self._cache_date = today
            return self._cached_analysis
        except Exception as error:
            _LOGGER.error(f"Error calculating shadow today analysis: {error}")
            self._cached_analysis = {
                "status": "error",
                "error": str(error),
                "shadow_hours": 0,
                "hourly_breakdown": [],
            }
            self._cache_date = today
            return self._cached_analysis

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates - invalidate cache. @zara"""
        self._cache_date = None
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Return if entity is available. @zara"""
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> Optional[float]:
        """Return number of shadow hours today. @zara"""
        analysis = self._get_today_analysis()
        return analysis.get("shadow_hours", 0)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return today's shadow analysis attributes. @zara"""
        if not _published_dev_mode_active(self):
            return {}
        analysis = self._get_today_analysis()
        if analysis.get("status") == "no_data":
            return {"status": "no_data", "hourly_breakdown": []}
        if analysis.get("status") == "error":
            return {"error": analysis.get("error", "unknown"), "hourly_breakdown": []}
        return {
            "shadow_hours_count": analysis.get("shadow_hours_count", 0),
            "shadow_hours": analysis.get("shadow_hours_list", []),
            "none_count": analysis.get("none_count", 0),
            "light_count": analysis.get("light_count", 0),
            "moderate_count": analysis.get("moderate_count", 0),
            "heavy_count": analysis.get("heavy_count", 0),
            "total_analyzed_hours": analysis.get("total_analyzed_hours", 0),
            "total_candidate_hours": analysis.get("total_candidate_hours", 0),
            "incomplete_hours_count": analysis.get("incomplete_hours_count", 0),
            "incomplete_hours_list": analysis.get("incomplete_hours_list", []),
            "peak_shadow_hour": analysis.get("peak_shadow_hour"),
            "peak_shadow_percent": analysis.get("peak_shadow_percent", 0),
            "cumulative_loss_kwh": analysis.get("cumulative_loss_kwh", 0),
            "cloud_hours": analysis.get("cloud_hours", 0),
            "cloud_hours_list": analysis.get("cloud_hours_list", []),
            "real_shadow_hours": analysis.get("real_shadow_hours", 0),
            "real_shadow_hours_list": analysis.get("real_shadow_hours_list", []),
            "candidate_hours": analysis.get("candidate_hours", 0),
            "candidate_hours_list": analysis.get("candidate_hours_list", []),
            "forecast_shadow_applied": analysis.get("forecast_shadow_applied", []),
            "low_sun_hours": analysis.get("low_sun_hours", 0),
            "low_sun_hours_list": analysis.get("low_sun_hours_list", []),
            "daily_loss_percent": analysis.get("daily_loss_percent", 0),
            "date": analysis.get("date", ""),
            "snow_hours": analysis.get("snow_hours", 0),
            "snow_hours_list": analysis.get("snow_hours_list", []),
            "hourly_breakdown": analysis.get("hourly_breakdown", []),
        }


class PerformanceLossTodaySensor(CoordinatorEntity, SensorEntity):
    """Sensor for today's performance loss due to shading. @zara"""

    def __init__(self, coordinator, entry):
        """Initialize the sensor. @zara"""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_has_entity_name = True
        self._attr_translation_key = "performance_loss_today"
        self._attr_unique_id = f"{entry.entry_id}_performance_loss_today"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_icon = "mdi:solar-power-variant"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Solar Forecast ML",
            manufacturer="Zara-Toorox",
            model=INTEGRATION_MODEL,
            sw_version=f"SW {SOFTWARE_VERSION} | AI {AI_VERSION}",
            configuration_url="https://github.com/Zara-Toorox/ha-solar-forecast-ml",
        )
        self._cached_analysis = None
        self._cache_date = None

    def _get_performance_analysis(self) -> Dict[str, Any]:
        """Calculate today's performance loss analysis with caching. @zara"""
        today = dt_util.now().date().isoformat()
        if self._cache_date == today and self._cached_analysis is not None:
            return self._cached_analysis
        try:
            today_predictions = _get_today_predictions_from_cache(self.coordinator)
            shadow_predictions = [
                prediction
                for prediction in today_predictions
                if prediction.get("shadow_detection") is not None
            ]
            daylight_predictions = _filter_valid_shadow_predictions(today_predictions)
            if not shadow_predictions:
                self._cached_analysis = {
                    "status": "no_data",
                    "total_loss_kwh": 0.0,
                    "loss_percent": 0,
                }
                self._cache_date = today
                return self._cached_analysis
            total_loss_kwh = 0.0
            for prediction in shadow_predictions:
                predicted_kwh = prediction.get("prediction_kwh")
                actual_kwh = prediction.get("actual_kwh")
                if isinstance(predicted_kwh, (int, float)) and isinstance(
                    actual_kwh, (int, float)
                ):
                    total_loss_kwh += max(0.0, predicted_kwh - actual_kwh)
            if not daylight_predictions:
                self._cached_analysis = {
                    "status": "partial",
                    "total_loss_kwh": round(total_loss_kwh, 3),
                    "loss_percent": 0,
                    "hours_analyzed": 0,
                }
                self._cache_date = today
                return self._cached_analysis
            total_actual_kwh = 0.0
            total_predicted_kwh = 0.0
            root_cause_counts = {}
            for daylight_prediction in daylight_predictions:
                actual_kwh = daylight_prediction.get("actual_kwh")
                if isinstance(actual_kwh, (int, float)) and actual_kwh >= 0:
                    total_actual_kwh += actual_kwh
                predicted_kwh = daylight_prediction.get("prediction_kwh")
                if isinstance(predicted_kwh, (int, float)) and predicted_kwh >= 0:
                    total_predicted_kwh += predicted_kwh
                shadow = _shadow_data(daylight_prediction)
                root_cause_key = _root_cause_key(shadow.get("root_cause", "unknown"))
                root_cause_display = _translate_root_cause(root_cause_key, self.hass)
                root_cause_counts[root_cause_display] = (
                    root_cause_counts.get(root_cause_display, 0) + 1
                )
            if total_predicted_kwh > 0:
                overall_efficiency_percent = (
                    total_actual_kwh / total_predicted_kwh * 100.0
                )
                loss_percent = total_loss_kwh / total_predicted_kwh * 100.0
            else:
                overall_efficiency_percent = 0.0
                loss_percent = 0.0
            dominant_cause = (
                max(root_cause_counts, key=root_cause_counts.get)
                if root_cause_counts
                else "unknown"
            )
            self._cached_analysis = {
                "total_loss_kwh": round(total_loss_kwh, 3),
                "total_actual_kwh": round(total_actual_kwh, 3),
                "total_predicted_kwh": round(total_predicted_kwh, 3),
                "overall_efficiency_percent": round(overall_efficiency_percent, 1),
                "loss_percent": round(loss_percent, 1),
                "root_causes": root_cause_counts,
                "dominant_cause": dominant_cause,
                "hours_analyzed": len(daylight_predictions),
                "date": today,
            }
            self._cache_date = today
            return self._cached_analysis
        except Exception as error:
            _LOGGER.error(f"Error calculating performance loss analysis: {error}")
            self._cached_analysis = {
                "status": "error",
                "error": str(error),
                "total_loss_kwh": 0.0,
                "loss_percent": 0,
            }
            self._cache_date = today
            return self._cached_analysis

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates - invalidate cache. @zara"""
        self._cache_date = None
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Return if entity is available. @zara"""
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> Optional[float]:
        """Return cumulative kWh lost today due to shading. @zara"""
        analysis = self._get_performance_analysis()
        return analysis.get("total_loss_kwh", 0.0)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return performance loss analysis attributes. @zara"""
        if not _published_dev_mode_active(self):
            return {}
        analysis = self._get_performance_analysis()
        if analysis.get("status") == "no_data":
            return {"status": "no_data"}
        if analysis.get("status") == "error":
            return {"error": analysis.get("error", "unknown")}
        return {
            "total_actual_kwh": analysis.get("total_actual_kwh", 0),
            "total_predicted_kwh": analysis.get("total_predicted_kwh", 0),
            "total_loss_kwh": analysis.get("total_loss_kwh", 0),
            "overall_efficiency_percent": analysis.get("overall_efficiency_percent", 0),
            "loss_percent": analysis.get("loss_percent", 0),
            "root_causes": analysis.get("root_causes", {}),
            "dominant_cause": analysis.get("dominant_cause", "unknown"),
            "hours_analyzed": analysis.get("hours_analyzed", 0),
            "date": analysis.get("date", ""),
        }

    @property
    def icon(self) -> str:
        """Return icon based on loss severity. @zara"""
        analysis = self._get_performance_analysis()
        loss_percent = analysis.get("loss_percent", 0)
        if not isinstance(loss_percent, (int, float)):
            return "mdi:solar-power-variant"
        if loss_percent < 10:
            return "mdi:solar-power"
        if loss_percent < 25:
            return "mdi:solar-power-variant"
        return "mdi:solar-power-variant-outline"


SHADOW_DETECTION_SENSORS = [
    ShadowCurrentSensor,
    ShadowTodaySensor,
    PerformanceLossTodaySensor,
]
