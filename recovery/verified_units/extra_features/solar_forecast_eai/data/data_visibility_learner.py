# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Visibility Learning System for Solar Forecast ML V16.2.0.
Implements visibility-based learning for fog detection and solar radiation attenuation.
Uses database operations via visibility_learning table.

@zara
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from .data_io import DataManagerIO
from ..const import CONF_SOLAR_RADIATION_SENSOR

_LOGGER = logging.getLogger(__name__)
TRANSMITTANCE_DENSE_FOG = 0.55
TRANSMITTANCE_LIGHT_FOG = 0.75
EXPECTED_TRANSMITTANCE_BY_CLOUD: Dict[str, float] = {
    "clear": 0.95,
    "cirrus": 0.85,
    "fair": 0.7,
    "mixed": 0.55,
    "stratus": 0.4,
    "overcast": 0.25,
}
FOG_TRANSMITTANCE_DEFICIT = 0.15
STATIC_FOG_WEIGHTS = {"bright_sky": 0.6, "pirate_weather": 0.4}
VISIBILITY_LEARNING_SMOOTHING = 0.25
VISIBILITY_MIN_HOURS = 3
VISIBILITY_MIN_GHI = 50.0


class VisibilityLearner(DataManagerIO):
    """Learns visibility/fog prediction accuracy from solar radiation measurements. @zara

    V16.0.0: All data persistence via visibility_learning database table.
    Replaces JSON file operations with database queries.

    This class tracks how well each weather source (Bright Sky DWD, Pirate Weather)
    predicts fog conditions by comparing visibility forecasts with actual solar
    radiation transmittance.

    When fog is present, solar radiation is significantly reduced. By measuring
    actual GHI vs clear-sky GHI, we can validate fog predictions and learn which
    source is more accurate for the user's location.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db_manager: DatabaseManager,
        config_entry: Optional[ConfigEntry] = None,
        astronomy_provider=None,
    ):
        "Initialize the VisibilityLearner. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager instance for DB operations\n    config_entry: Configuration entry for sensor checks\n"
        super().__init__(hass, db_manager)
        self.config_entry = config_entry
        self._astronomy_provider = astronomy_provider
        self.has_solar_radiation_sensor = False
        if config_entry and config_entry.data:
            self.has_solar_radiation_sensor = (
                config_entry.data.get(CONF_SOLAR_RADIATION_SENSOR) is not None
            )
        self._fog_weights = {
            "fog": STATIC_FOG_WEIGHTS.copy(),
            "fog_light": STATIC_FOG_WEIGHTS.copy(),
        }
        self._learning_stats = {
            "last_learning_date": None,
            "total_fog_hours_learned": 0,
            "total_fog_light_hours_learned": 0,
            "bright_sky_fog_hits": 0,
            "pirate_weather_fog_hits": 0,
            "learning_sessions": 0,
        }
        _LOGGER.info(
            "VisibilityLearner initialized (solar_radiation_sensor=%s)",
            "configured" if self.has_solar_radiation_sensor else "not configured",
        )
        return None if True else None

    @property
    def is_learning_active(self) -> bool:
        """Check if active learning is enabled. @zara

        Learning is only active if a solar_radiation sensor is configured.
        """
        return self.has_solar_radiation_sensor

    async def initialize(self) -> bool:
        "Initialize the visibility learner and load saved data from DB. @zara\n\nReturns:\n    True if initialization successful, False otherwise.\n"
        try:
            await self.ensure_initialized()
            database_row = await self.fetch_one(
                "SELECT visibility_threshold_m, fog_visibility_threshold_m,\n                          samples_below_threshold, samples_above_threshold\n                   FROM visibility_learning WHERE id = 1"
            )
            if database_row:
                self._learning_stats["samples_below_threshold"] = database_row[2] or 0
                self._learning_stats["samples_above_threshold"] = database_row[3] or 0
                _LOGGER.debug(
                    "VisibilityLearner loaded from DB: below=%d, above=%d",
                    database_row[2] or 0,
                    database_row[3] or 0,
                )
            else:
                _LOGGER.debug("VisibilityLearner: No saved data, using defaults")
            await self._load_fog_weights()
        except Exception as error:
            _LOGGER.error("VisibilityLearner initialization failed: %s", error)
            return False
        return True

    async def _load_fog_weights(self) -> None:
        """Load fog weights from weather_expert_weights table. @zara"""
        try:
            for fog_type in ("fog", "fog_light"):
                saved_weights = await self.db.get_weather_expert_weights(fog_type)
                if saved_weights:
                    self._fog_weights[fog_type] = saved_weights
                    _LOGGER.debug(
                        "Loaded %s weights from DB: %s", fog_type, saved_weights
                    )
        except Exception as error:
            _LOGGER.warning("Failed to load fog weights from DB: %s", error)
        return None if True else None

    def get_visibility_weights(self) -> Dict[str, Dict[str, float]]:
        "Get current visibility/fog weights for both fog types. @zara\n\nReturns:\n    Dict with 'fog' and 'fog_light' keys, each containing source weights.\n"
        return {
            "fog": self._fog_weights.get("fog", STATIC_FOG_WEIGHTS).copy(),
            "fog_light": self._fog_weights.get("fog_light", STATIC_FOG_WEIGHTS).copy(),
        }

    def get_fog_weight(self, cloud_type: str) -> Dict[str, float]:
        "Get fog weights for a specific cloud type (fog or fog_light). @zara\n\nThis method is called by WeatherExpertBlender.get_weights_for_cloud_type()\nto get specialized weights for fog conditions.\n\nArgs:\n    cloud_type: Either 'fog' or 'fog_light'\n\nReturns:\n    Dict mapping source name to weight (0-1)\n"
        if cloud_type not in ("fog", "fog_light"):
            return STATIC_FOG_WEIGHTS.copy()
        if not self.has_solar_radiation_sensor:
            return STATIC_FOG_WEIGHTS.copy()
        return self._fog_weights.get(cloud_type, STATIC_FOG_WEIGHTS).copy()

    async def learn_from_day(self, date_str: str) -> Dict[str, Any]:
        """Learn visibility weights from a day's actual observations. @zara

        Compares visibility predictions from Bright Sky and Pirate Weather
        with actual transmittance calculated from solar radiation measurements.

        Args:
            date_str: Date to learn from (YYYY-MM-DD format)

        Returns:
            Dict with learning results and statistics
        """
        result = {
            "success": False,
            "date": date_str,
            "reason": None,
            "fog_hours_detected": 0,
            "fog_light_hours_detected": 0,
            "bright_sky_accuracy": None,
            "pirate_weather_accuracy": None,
            "weights_updated": False,
        }
        if not self.has_solar_radiation_sensor:
            result["reason"] = "No solar radiation sensor - using static weights"
            result["success"] = True
            return result
        try:
            actual_by_hour = await self._load_actual_weather_from_db(date_str)
            if not actual_by_hour:
                result["reason"] = "No actual weather data available"
                return result
            astronomy_by_hour = await self._load_astronomy_from_db(date_str)
            if not astronomy_by_hour:
                result["reason"] = "No astronomy data for clear-sky reference"
                return result
            forecast_visibility = await self._load_forecast_visibility(date_str)
            dense_fog_samples = []
            light_fog_samples = []
            bright_sky_hits = 0
            bright_sky_samples = 0
            pirate_weather_hits = 0
            pirate_weather_samples = 0
            for hour, actual_weather in actual_by_hour.items():
                actual_ghi_wm2 = actual_weather.get("solar_radiation_wm2")
                if actual_ghi_wm2 is None or actual_ghi_wm2 < VISIBILITY_MIN_GHI:
                    continue
                clear_sky_ghi_wm2 = astronomy_by_hour.get(hour, {}).get(
                    "clear_sky_radiation_wm2"
                )
                if clear_sky_ghi_wm2 is None or clear_sky_ghi_wm2 < VISIBILITY_MIN_GHI:
                    continue
                transmittance = min(1.0, actual_ghi_wm2 / clear_sky_ghi_wm2)
                cloud_cover_percent = actual_weather.get("cloud_cover_percent", 50)
                expected_transmittance = self._get_expected_transmittance(
                    cloud_cover_percent
                )
                transmittance_deficit = expected_transmittance - transmittance
                fog_type = None
                if (
                    transmittance < TRANSMITTANCE_DENSE_FOG
                    and transmittance_deficit > FOG_TRANSMITTANCE_DEFICIT
                ):
                    fog_type = "fog"
                elif (
                    transmittance < TRANSMITTANCE_LIGHT_FOG
                    and transmittance_deficit > FOG_TRANSMITTANCE_DEFICIT
                ):
                    fog_type = "fog_light"
                if fog_type is None:
                    continue
                visibility = forecast_visibility.get(hour, {})
                bright_sky_visibility_m = visibility.get("bright_sky_visibility_m")
                pirate_weather_visibility_m = visibility.get(
                    "pirate_weather_visibility_m"
                )
                fog_sample = {
                    "hour": hour,
                    "actual_ghi": round(actual_ghi_wm2, 1),
                    "clear_sky_ghi": round(clear_sky_ghi_wm2, 1),
                    "transmittance": round(transmittance * 100, 1),
                    "expected_transmittance": round(expected_transmittance * 100, 1),
                    "fog_type": fog_type,
                }
                if fog_type == "fog":
                    dense_fog_samples.append(fog_sample)
                else:
                    light_fog_samples.append(fog_sample)
                if bright_sky_visibility_m is not None:
                    bright_sky_samples += 1
                    if fog_type == "fog" and bright_sky_visibility_m < 1000:
                        bright_sky_hits += 1
                    elif fog_type == "fog_light" and bright_sky_visibility_m < 5000:
                        bright_sky_hits += 1
                if pirate_weather_visibility_m is not None:
                    pirate_weather_samples += 1
                    if fog_type == "fog" and pirate_weather_visibility_m < 1000:
                        pirate_weather_hits += 1
                    elif fog_type == "fog_light" and pirate_weather_visibility_m < 5000:
                        pirate_weather_hits += 1
            result["fog_hours_detected"] = len(dense_fog_samples)
            result["fog_light_hours_detected"] = len(light_fog_samples)
            if bright_sky_samples > 0:
                result["bright_sky_accuracy"] = round(
                    bright_sky_hits / bright_sky_samples * 100, 1
                )
            if pirate_weather_samples > 0:
                result["pirate_weather_accuracy"] = round(
                    pirate_weather_hits / pirate_weather_samples * 100, 1
                )
            fog_hours_total = len(dense_fog_samples) + len(light_fog_samples)
            if fog_hours_total >= VISIBILITY_MIN_HOURS:
                await self._update_weights_from_accuracy(
                    bright_sky_hits,
                    bright_sky_samples,
                    pirate_weather_hits,
                    pirate_weather_samples,
                    len(dense_fog_samples),
                    len(light_fog_samples),
                )
                result["weights_updated"] = True
                self._learning_stats["last_learning_date"] = date_str
                self._learning_stats["total_fog_hours_learned"] += len(
                    dense_fog_samples
                )
                self._learning_stats["total_fog_light_hours_learned"] += len(
                    light_fog_samples
                )
                self._learning_stats["bright_sky_fog_hits"] += bright_sky_hits
                self._learning_stats["pirate_weather_fog_hits"] += pirate_weather_hits
                self._learning_stats["learning_sessions"] += 1
                await self._save_learning_data()
            result["success"] = True
            result["reason"] = f"Analyzed {fog_hours_total} fog hours"
            if fog_hours_total > 0:
                _LOGGER.info(
                    "VisibilityLearner for %s: %d fog, %d fog_light hours. Accuracy: BrightSky=%.1f%%, PirateWeather=%.1f%%",
                    date_str,
                    len(dense_fog_samples),
                    len(light_fog_samples),
                    result["bright_sky_accuracy"] or 0,
                    result["pirate_weather_accuracy"] or 0,
                )
            else:
                _LOGGER.debug(
                    "VisibilityLearner for %s: No fog conditions detected", date_str
                )
            return result if True else None
        except Exception as error:
            result["reason"] = f"Error: {str(error)}"
            _LOGGER.error("Error in visibility learning: %s", error, exc_info=True)
            return result

    def _get_expected_transmittance(self, cloud_cover: float) -> float:
        "Get expected transmittance based on cloud cover. @zara\n\nArgs:\n    cloud_cover: Cloud cover percentage (0-100)\n\nReturns:\n    Expected transmittance (0-1)\n"
        if cloud_cover < 10:
            return EXPECTED_TRANSMITTANCE_BY_CLOUD["clear"]
        if cloud_cover < 30:
            return EXPECTED_TRANSMITTANCE_BY_CLOUD["cirrus"]
        if cloud_cover < 50:
            return EXPECTED_TRANSMITTANCE_BY_CLOUD["fair"]
        if cloud_cover < 70:
            return EXPECTED_TRANSMITTANCE_BY_CLOUD["mixed"]
        if cloud_cover < 90:
            return EXPECTED_TRANSMITTANCE_BY_CLOUD["stratus"]
        return EXPECTED_TRANSMITTANCE_BY_CLOUD["overcast"]

    async def _update_weights_from_accuracy(
        self,
        bs_correct: int,
        bs_total: int,
        pw_correct: int,
        pw_total: int,
        fog_hours: int,
        fog_light_hours: int,
    ) -> None:
        """Update fog weights based on source accuracy. @zara

        Uses exponential smoothing to gradually adapt weights.
        """
        bright_sky_accuracy = bs_correct / max(bs_total, 1)
        pirate_weather_accuracy = pw_correct / max(pw_total, 1)
        if bs_total == 0 and pw_total == 0:
            return
        combined_accuracy = bright_sky_accuracy + pirate_weather_accuracy
        if combined_accuracy > 0:
            target_bright_sky_weight = bright_sky_accuracy / combined_accuracy
            target_pirate_weather_weight = pirate_weather_accuracy / combined_accuracy
        else:
            target_bright_sky_weight = 0.5
            target_pirate_weather_weight = 0.5
        for fog_type in ("fog", "fog_light"):
            previous_weights = self._fog_weights.get(
                fog_type, STATIC_FOG_WEIGHTS
            ).copy()
            smoothing = VISIBILITY_LEARNING_SMOOTHING
            old_bright_sky_weight = previous_weights.get("bright_sky", 0.6)
            old_pirate_weather_weight = previous_weights.get("pirate_weather", 0.4)
            new_bright_sky_weight = (
                smoothing * target_bright_sky_weight
                + (1 - smoothing) * old_bright_sky_weight
            )
            new_pirate_weather_weight = (
                smoothing * target_pirate_weather_weight
                + (1 - smoothing) * old_pirate_weather_weight
            )
            weight_sum = new_bright_sky_weight + new_pirate_weather_weight
            if weight_sum > 0:
                new_bright_sky_weight /= weight_sum
                new_pirate_weather_weight /= weight_sum
            self._fog_weights[fog_type] = {
                "bright_sky": round(new_bright_sky_weight, 4),
                "pirate_weather": round(new_pirate_weather_weight, 4),
            }
            await self._save_expert_weights(fog_type, self._fog_weights[fog_type])
            continue
        _LOGGER.debug(
            "Updated fog weights: fog=%s, fog_light=%s",
            self._fog_weights["fog"],
            self._fog_weights["fog_light"],
        )

    async def _save_expert_weights(
        self, cloud_type: str, weights: Dict[str, float]
    ) -> None:
        """Save expert weights to database. @zara"""
        try:
            await self.db.save_weather_expert_weights(
                {
                    "weights": {cloud_type: weights},
                    "metadata": {"last_updated": datetime.now().isoformat()},
                }
            )
        except Exception as error:
            _LOGGER.error("Failed to save expert weights: %s", error)
        return None if True else None

    async def _load_actual_weather_from_db(
        self, date_str: str
    ) -> Dict[int, Dict[str, Any]]:
        "Load actual weather observations from database. @zara\n\nReturns:\n    Dict mapping hour -> weather data\n"
        try:
            database_rows = await self.fetch_all(
                "SELECT hp.target_hour, psa.solar_radiation_wm2,\n                          pwf.clouds as cloud_cover_percent\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_sensor_actual psa\n                       ON hp.prediction_id = psa.prediction_id\n                   LEFT JOIN prediction_weather pwf\n                       ON hp.prediction_id = pwf.prediction_id\n                       AND pwf.weather_type = 'actual'\n                   WHERE hp.target_date = ?\n                     AND psa.solar_radiation_wm2 IS NOT NULL",
                (date_str,),
            )
            actual_by_hour = {}
            for database_row in database_rows:
                hour = database_row[0]
                actual_by_hour[hour] = {
                    "solar_radiation_wm2": database_row[1],
                    "cloud_cover_percent": database_row[2] or 50,
                }
            return actual_by_hour
        except Exception as error:
            _LOGGER.warning("Error loading actual weather from DB: %s", error)
            return {}

    async def _load_astronomy_from_db(self, date_str: str) -> Dict[int, Dict[str, Any]]:
        "Load astronomy data from the read-only SFML provider.\n\nReturns:\n    Dict mapping hour -> astronomy data\n"
        try:
            astronomy_by_hour = {}
            astronomy_days = (
                await self._astronomy_provider.async_get_legacy_days(
                    datetime.fromisoformat(date_str).date(), days=1
                )
                if self._astronomy_provider
                else {}
            )
            for hour in range(24):
                astronomy_hour = astronomy_days.get(f"{date_str}_{hour:02d}")
                if astronomy_hour:
                    astronomy_by_hour[hour] = {
                        "clear_sky_radiation_wm2": astronomy_hour.get(
                            "clear_sky_radiation_wm2"
                        ),
                        "hour": hour,
                    }
            return astronomy_by_hour
        except Exception as error:
            _LOGGER.warning("Error loading SFML astronomy: %s", error)
            return {}

    async def _load_forecast_visibility(
        self, date_str: str
    ) -> Dict[int, Dict[str, Any]]:
        "Load visibility forecasts from weather_forecast table. @zara\n\nReturns:\n    Dict mapping hour -> visibility data\n"
        try:
            database_rows = await self.fetch_all(
                "SELECT hour, visibility_m\n                   FROM weather_forecast\n                   WHERE forecast_date = ?",
                (date_str,),
            )
            visibility_by_hour = {}
            for database_row in database_rows:
                hour = database_row[0]
                visibility_m = database_row[1]
                visibility_by_hour[hour] = {
                    "bright_sky_visibility_m": visibility_m,
                    "pirate_weather_visibility_m": visibility_m,
                }
            return visibility_by_hour
        except Exception as error:
            _LOGGER.warning("Error loading forecast visibility: %s", error)
            return {}

    async def _save_learning_data(self) -> bool:
        "Save learned data to visibility_learning table. @zara\n\nReturns:\n    True if save successful, False otherwise.\n"
        try:
            dense_fog_hours = self._learning_stats.get("total_fog_hours_learned", 0)
            light_fog_hours = self._learning_stats.get(
                "total_fog_light_hours_learned", 0
            )
            await self.execute_query(
                "INSERT INTO visibility_learning\n                   (id, samples_below_threshold, samples_above_threshold, last_updated)\n                   VALUES (1, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       samples_below_threshold = excluded.samples_below_threshold,\n                       samples_above_threshold = excluded.samples_above_threshold,\n                       last_updated = excluded.last_updated",
                (dense_fog_hours, light_fog_hours, datetime.now()),
            )
            _LOGGER.debug("Saved visibility learning data to DB")
        except Exception as error:
            _LOGGER.error("Error saving visibility learning data: %s", error)
            return False
        return True

    def get_learning_stats(self) -> Dict[str, Any]:
        "Get current learning statistics. @zara\n\nReturns:\n    Dict with learning statistics\n"
        return {
            **self._learning_stats,
            "current_weights": self._fog_weights.copy(),
            "has_solar_radiation_sensor": self.has_solar_radiation_sensor,
        }

    async def update_visibility_sample(self, visibility_m: float, is_fog: bool) -> None:
        "Update visibility sample counters in database. @zara\n\nArgs:\n    visibility_m: Measured visibility in meters\n    is_fog: Whether fog was detected\n"
        try:
            if is_fog:
                await self.execute_query(
                    "UPDATE visibility_learning\n                       SET samples_below_threshold = samples_below_threshold + 1,\n                           last_updated = ?\n                       WHERE id = 1",
                    (datetime.now(),),
                )
            else:
                await self.execute_query(
                    "UPDATE visibility_learning\n                       SET samples_above_threshold = samples_above_threshold + 1,\n                           last_updated = ?\n                       WHERE id = 1",
                    (datetime.now(),),
                )
        except Exception as error:
            _LOGGER.error("Failed to update visibility sample: %s", error)
        return None if True else None
