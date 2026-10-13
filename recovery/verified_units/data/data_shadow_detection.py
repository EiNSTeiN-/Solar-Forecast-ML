# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Shadow Detection for Solar Forecast ML V16.2.0.
Detects and analyzes solar array shading using multiple methods.
Uses database operations for persistence.
Now includes learned pattern integration for improved accuracy.

@zara
"""
import logging
import math
from datetime import date, timedelta
from statistics import median
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from .data_io import DataManagerIO
from .data_shadow_group_math import (
    GROUP_GLOBAL_WEATHER_RATIO_MAX as DEFAULT_GROUP_GLOBAL_WEATHER_RATIO_MAX,
    GROUP_PEER_GAP_MIN as DEFAULT_GROUP_PEER_GAP_MIN,
    GROUP_PEER_RATIO_MAX as DEFAULT_GROUP_PEER_RATIO_MAX,
    MIN_GROUP_EXPECTED_KWH as DEFAULT_MIN_GROUP_EXPECTED_KWH,
    compare_panel_group_performance,
)
from .data_shadow_observation import (
    CEILING_WINDOW_DAYS,
    OBSERVATION_NOT_EVALUABLE,
    build_geometry_ceilings,
    classify_observation_class,
    classify_sensorless_hour,
    elevation_bin,
    pyranometer_is_implausible,
)
from ..const import CURTAILMENT_THROTTLE_REASON_SQL, CURTAILMENT_THROTTLE_REASONS
from ..core.core_helpers import SafeDateTimeUtil

if TYPE_CHECKING:
    from .data_shadow_pattern_learning import ShadowPatternLearner
_LOGGER = logging.getLogger(__name__)


def shadow_flags_mark_production_limited(flags: Optional[Dict[str, Any]]) -> bool:
    """Return True when this hour must not be read as local shading.

    Curtailment evidence is wider than the learning quarantine on purpose:
    a false positive here drops one shadow observation, not a learning hour.
    """
    flow_flags = flags or {}
    if (
        flow_flags.get("inverter_clipped")
        or flow_flags.get("mppt_throttled")
        or flow_flags.get("partial_panel_group_actuals")
        or flow_flags.get("curtailment_suspected")
    ):
        return True
    throttle_reason = flow_flags.get("mppt_throttle_reason")
    return bool(throttle_reason) and throttle_reason in CURTAILMENT_THROTTLE_REASONS


def _shadow_loss_percent(actual_kwh: float, theoretical_max: float) -> float:
    if theoretical_max <= 0.0:
        return 0.0
    return min(100.0, max(0.0, (1.0 - actual_kwh / theoretical_max) * 100.0))


class ShadowDetector(DataManagerIO):
    """Detects and analyzes solar array shading using multiple methods. @zara

    V16.2.0 Methods:
    1. Theory Ratio - Compare actual vs theoretical_max (universal, fast)
    2. Correlation-Based Detection - Compare irradiance/Lux drop vs production drop
    3. Ensemble - Weighted combination of both methods
    4. Pattern Learning - Uses learned patterns to improve confidence

    Uses a pyranometer first and Lux as fallback to distinguish cloud cover
    from local shadows.
    """

    SHADOW_THRESHOLD_LIGHT = 0.05
    SHADOW_THRESHOLD_MODERATE = 0.15
    SHADOW_THRESHOLD_HEAVY = 0.3
    CORRELATION_THRESHOLD_BASE = 0.15
    IRRADIANCE_CORRELATION_THRESHOLD_BASE = 0.12
    MAX_SOLAR_RADIATION_WM2 = 2000.0
    MIN_CLEAR_SKY_RADIATION_WM2 = 20.0
    MIN_GROUP_EXPECTED_KWH = DEFAULT_MIN_GROUP_EXPECTED_KWH
    GROUP_PEER_RATIO_MAX = DEFAULT_GROUP_PEER_RATIO_MAX
    GROUP_PEER_GAP_MIN = DEFAULT_GROUP_PEER_GAP_MIN
    GROUP_GLOBAL_WEATHER_RATIO_MAX = DEFAULT_GROUP_GLOBAL_WEATHER_RATIO_MAX
    WEIGHT_THEORY = 0.6
    WEIGHT_SENSOR = 0.4

    def __init__(
        self,
        hass: HomeAssistant,
        db_manager: DatabaseManager,
        panel_groups: Optional[List[Dict[str, Any]]] = None,
    ):
        "Initialize shadow detector. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager instance for DB operations\n    panel_groups: Optional list of panel group configurations\n"
        super().__init__(hass, db_manager)
        self._panel_groups = panel_groups or []
        self._use_panel_groups = bool(panel_groups)
        self._pattern_learner = None
        if self._use_panel_groups:
            _LOGGER.debug(
                "ShadowDetector initialized with %d panel groups",
                len(self._panel_groups),
            )
        else:
            _LOGGER.debug(
                "ShadowDetector initialized (V16.2.0: Pattern Learning Enabled)"
            )
        return None if True else None

    def set_pattern_learner(self, learner: "ShadowPatternLearner") -> None:
        "Set the pattern learner for learned pattern integration. @zara V16.2\n\nArgs:\n    learner: ShadowPatternLearner instance\n"
        self._pattern_learner = learner
        _LOGGER.debug("Shadow pattern learner connected to ShadowDetector")

    async def _ensure_pattern_learner(self) -> None:
        if self._pattern_learner and self._pattern_learner._initialized:
            return
        try:
            from .data_shadow_pattern_learning import (
                get_shadow_pattern_learner as get_shadow_pattern_learner,
            )

            learner = get_shadow_pattern_learner(self.hass, self.db)
            if not learner._initialized:
                if not await learner.async_init():
                    return
            self.set_pattern_learner(learner)
        except Exception as initialization_error:
            _LOGGER.debug(
                "Shadow pattern learner is not available: %s", initialization_error
            )
        return None if True else None

    async def detect_shadow_theory_ratio(
        self, prediction: Dict[str, Any]
    ) -> Dict[str, Any]:
        "Method 1: Theory Ratio - Fast and Universal. @zara\n\nCompares actual production with theoretical maximum from clear sky model.\nWorks WITHOUT any external sensors (astronomy cache only).\n\nArgs:\n    prediction: Hourly prediction dict with actual_kwh and astronomy data\n\nReturns:\n    Shadow detection result with classification and confidence\n"
        try:
            actual_kwh = self._first_numeric(prediction.get("actual_kwh"))
            astronomy = prediction.get("astronomy") or {}
            theoretical_max_kwh = self._first_numeric(
                astronomy.get("theoretical_max_kwh")
            )
            if (
                actual_kwh is None
                or actual_kwh < 0.0
                or theoretical_max_kwh is None
                or (theoretical_max_kwh < 0.0)
            ):
                return self._not_evaluable_theory_result(
                    "invalid_energy_reference",
                    "Actual or theoretical energy is unavailable or invalid",
                )
            if theoretical_max_kwh < 0.01:
                return {
                    "method": "theory_ratio",
                    "shadow_type": "night",
                    "shadow_percent": 100.0,
                    "confidence": 1.0,
                    "efficiency_ratio": 0.0,
                    "interpretation": "No solar production expected (sun below horizon)",
                }
            efficiency_ratio = actual_kwh / theoretical_max_kwh
            loss_percent = _shadow_loss_percent(actual_kwh, theoretical_max_kwh)
            loss_fraction = loss_percent / 100.0
            if loss_fraction < self.SHADOW_THRESHOLD_LIGHT:
                shadow_type = "none"
                confidence = 0.95
                interpretation = "Excellent production - minimal losses"
                possible_causes = []
                root_cause = "normal_variation"
            elif loss_fraction < self.SHADOW_THRESHOLD_MODERATE:
                shadow_type = "light"
                confidence = 0.85
                interpretation = "Light shadowing or atmospheric effects"
                possible_causes = [
                    "Thin clouds",
                    "Dust/pollen on panels",
                    "Morning/evening haze",
                ]
                root_cause = "unknown"
            elif loss_fraction < self.SHADOW_THRESHOLD_HEAVY:
                shadow_type = "moderate"
                confidence = 0.8
                interpretation = "Moderate shadowing detected"
                possible_causes = [
                    "Clouds",
                    "Building shadows",
                    "Horizon obstruction",
                    "Panel soiling",
                ]
                root_cause = "unknown"
            else:
                shadow_type = "heavy"
                confidence = 0.75
                interpretation = "Heavy shadowing or system issue"
                possible_causes = [
                    "Dense clouds",
                    "Major obstruction",
                    "Rain",
                    "Snow on panels",
                    "System fault",
                ]
                root_cause = "unknown"
            loss_kwh = theoretical_max_kwh - actual_kwh
            result = {
                "method": "theory_ratio",
                "shadow_type": shadow_type,
                "shadow_percent": round(loss_percent, 1),
                "confidence": confidence,
                "efficiency_ratio": round(efficiency_ratio, 3),
                "loss_kwh": round(loss_kwh, 3),
                "loss_percent": round(loss_percent, 1),
                "interpretation": interpretation,
                "possible_causes": possible_causes,
                "root_cause": root_cause,
                "theoretical_max_kwh": theoretical_max_kwh,
                "actual_kwh": actual_kwh,
            }
            per_group_theory = astronomy.get("theoretical_max_per_group", [])
            if per_group_theory and self._use_panel_groups:
                group_analysis = self._analyze_per_group_shadows(
                    actual_kwh, theoretical_max_kwh, per_group_theory
                )
                result["per_group_analysis"] = group_analysis
            return result
        except Exception as error:
            _LOGGER.error(
                "Theory ratio shadow detection failed: %s", error, exc_info=True
            )
            return {
                "method": "theory_ratio",
                "error": str(error),
                "shadow_type": "error",
                "confidence": 0.0,
            }

    def _analyze_per_group_shadows(
        self,
        actual_kwh: float,
        theoretical_max_total: float,
        per_group_data: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        "Analyze shadow detection per panel group. @zara\n\nSince we only have total actual_kwh, we estimate per-group performance\nby assuming the ratio is proportional to theoretical max per group.\n\nArgs:\n    actual_kwh: Total actual production\n    theoretical_max_total: Total theoretical maximum\n    per_group_data: Per-group astronomy data\n\nReturns:\n    List of per-group shadow analysis\n"
        group_analysis = []
        if not per_group_data or theoretical_max_total <= 0:
            return group_analysis
        group_theory_total = sum(
            (group_data.get("theoretical_kwh", 0) for group_data in per_group_data)
        )
        if group_theory_total <= 0:
            return group_analysis
        system_efficiency = (
            actual_kwh / theoretical_max_total if theoretical_max_total > 0 else 0
        )
        for group_data in per_group_data:
            group_name = group_data.get("name", "Unknown")
            group_theory = group_data.get("theoretical_kwh", 0)
            tilt = group_data.get("tilt_deg", 30)
            azimuth = group_data.get("azimuth_deg", 180)
            group_actual = group_data.get("actual_kwh")
            if group_actual is not None and group_theory > 0:
                group_efficiency = group_actual / group_theory
                estimated_actual = group_actual
            else:
                group_efficiency = system_efficiency
                estimated_actual = group_theory * system_efficiency
            group_loss_percent = (
                (1.0 - group_efficiency) * 100 if group_efficiency < 1.0 else 0
            )
            if group_loss_percent < 5:
                shadow_type = "none"
                interpretation = "Normal production"
            elif group_loss_percent < 15:
                shadow_type = "light"
                interpretation = "Light shading"
            elif group_loss_percent < 30:
                shadow_type = "moderate"
                interpretation = "Moderate shading"
            else:
                shadow_type = "heavy"
                interpretation = "Heavy shading"
            group_analysis.append(
                {
                    "name": group_name,
                    "tilt_deg": tilt,
                    "azimuth_deg": azimuth,
                    "theoretical_kwh": round(group_theory, 4),
                    "estimated_actual_kwh": round(estimated_actual, 4),
                    "shadow_type": shadow_type,
                    "shadow_percent": round(group_loss_percent, 1),
                    "interpretation": interpretation,
                }
            )
        return group_analysis

    async def detect_shadow_sensor_fusion(
        self, prediction: Dict[str, Any]
    ) -> Dict[str, Any]:
        "Method 2: Correlation-Based Detection - High Accuracy. @zara\n\nSource priority:\n1. On-site pyranometer\n2. Outdoor Lux sensor\n3. Cloud-cover fallback\n\nArgs:\n    prediction: Hourly prediction dict with weather, astronomy, and sensor data\n\nReturns:\n    Shadow detection result with cloud/obstruction classification\n"
        try:
            actual_weather = prediction.get("weather_actual") or {}
            corrected_weather = prediction.get("weather_corrected") or {}
            forecast_weather = prediction.get("weather_forecast") or {}
            astronomy = prediction.get("astronomy") or {}
            sensor_readings = prediction.get("sensor_actual", {})
            flags = prediction.get("flags") or {}
            if shadow_flags_mark_production_limited(flags):
                return self._not_evaluable_fusion_result(
                    "production_limited",
                    "production_limited",
                    "Production is limited or panel-group actuals are incomplete",
                )
            cloud_cover = self._first_numeric(
                actual_weather.get("cloud_cover_percent"),
                actual_weather.get("clouds"),
                corrected_weather.get("clouds"),
                corrected_weather.get("cloud_cover"),
                forecast_weather.get("clouds"),
                forecast_weather.get("cloud_cover"),
            )
            actual_kwh = self._first_numeric(prediction.get("actual_kwh"))
            theoretical_max_kwh = self._first_numeric(
                astronomy.get("theoretical_max_kwh")
            )
            clear_sky_radiation = self._first_numeric(
                astronomy.get("clear_sky_radiation_wm2")
            )
            sun_elevation = self._first_numeric(
                astronomy.get("sun_elevation_deg"), astronomy.get("elevation_deg")
            )
            temperature = self._first_numeric(
                actual_weather.get("temperature_c"),
                actual_weather.get("temperature"),
                corrected_weather.get("temperature"),
                forecast_weather.get("temperature"),
            )
            if temperature is None:
                temperature = 15.0
            if (
                actual_kwh is None
                or actual_kwh < 0.0
                or theoretical_max_kwh is None
                or (theoretical_max_kwh < 0.0)
                or (sun_elevation is None)
            ):
                return self._not_evaluable_fusion_result(
                    "invalid_energy_reference",
                    "invalid_reference",
                    "Actual, theoretical, or solar-position data is invalid",
                )
            if theoretical_max_kwh < 0.01:
                return {
                    "method": "sensor_fusion",
                    "shadow_type": "night",
                    "confidence": 1.0,
                    "mode": "night",
                }
            if clear_sky_radiation is None or clear_sky_radiation <= 0:
                return self._not_evaluable_fusion_result(
                    "invalid_clear_sky_reference",
                    "invalid_reference",
                    "Clear-sky irradiance reference is unavailable or invalid",
                )
            sensor_radiation = self._first_numeric(
                sensor_readings.get("solar_radiation_wm2")
            )
            radiation_valid = (
                sensor_radiation is not None
                and 0.0 <= sensor_radiation <= self.MAX_SOLAR_RADIATION_WM2
            )
            if radiation_valid:
                if pyranometer_is_implausible(
                    sensor_radiation,
                    clear_sky_radiation,
                    actual_kwh,
                    theoretical_max_kwh,
                ):
                    return self._not_evaluable_fusion_result(
                        "implausible_radiation_sensor",
                        "solar_radiation_sensor_dead",
                        "Radiation sensor reports near-zero against a clear-sky reference",
                    )
                return await self._detect_with_irradiance_correlation(
                    cloud_cover,
                    clear_sky_radiation,
                    sensor_radiation,
                    actual_kwh,
                    theoretical_max_kwh,
                    sun_elevation,
                    temperature,
                )
            sensor_lux = self._first_numeric(
                sensor_readings.get("lux"), actual_weather.get("lux")
            )
            lux_valid = sensor_lux is not None and sensor_lux >= 0
            if lux_valid:
                return await self._detect_with_lux_correlation(
                    prediction,
                    cloud_cover or 0.0,
                    clear_sky_radiation,
                    sensor_lux,
                    actual_kwh,
                    theoretical_max_kwh,
                    sun_elevation,
                    temperature,
                )
            return await self._detect_with_cloud_cover(
                prediction, cloud_cover, actual_kwh, theoretical_max_kwh
            )
        except Exception as error:
            _LOGGER.error(
                "Sensor fusion shadow detection failed: %s", error, exc_info=True
            )
            return {
                "method": "sensor_fusion",
                "error": str(error),
                "shadow_type": "error",
                "confidence": 0.0,
            }

    @staticmethod
    def _first_numeric(*values: Any) -> Optional[float]:
        for value in values:
            if value is None or isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue
            return number

    @staticmethod
    def _not_evaluable_theory_result(
        reason: str, interpretation: str
    ) -> Dict[str, Any]:
        return {
            "method": "theory_ratio",
            "shadow_type": "not_evaluable",
            "shadow_percent": 0.0,
            "confidence": 0.0,
            "efficiency_ratio": 0.0,
            "loss_kwh": 0.0,
            "theoretical_max_kwh": 0.0,
            "root_cause": "insufficient_reference",
            "interpretation": interpretation,
            "evaluable": False,
            "learning_eligible": False,
            "not_evaluable_reason": reason,
        }

    @staticmethod
    def _not_evaluable_fusion_result(
        reason: str, mode: str, interpretation: str
    ) -> Dict[str, Any]:
        """Return a fail-closed result that cannot be used as shadow evidence."""
        return {
            "method": "sensor_fusion",
            "mode": mode,
            "shadow_type": "not_evaluable",
            "shadow_percent": 0.0,
            "confidence": 0.0,
            "root_cause": "insufficient_reference",
            "interpretation": interpretation,
            "evaluable": False,
            "learning_eligible": False,
            "not_evaluable_reason": reason,
        }

    async def _detect_with_irradiance_correlation(
        self,
        cloud_cover: Optional[float],
        clear_sky_radiation: float,
        sensor_radiation: float,
        actual_kwh: float,
        theoretical_max: float,
        sun_elevation: float,
        temperature: float,
    ) -> Dict[str, Any]:
        if sun_elevation < 10:
            return (
                (
                    self._not_evaluable_fusion_result(
                        "low_sun_angle",
                        "solar_radiation_sensor_low_angle",
                        f"Sun elevation too low ({sun_elevation:.1f}deg) for reliable irradiance correlation",
                    )
                    if True
                    else None
                )
                if True
                else None
            )
        if temperature < 0 and actual_kwh < theoretical_max * 0.15:
            efficiency_ratio = (
                actual_kwh / theoretical_max if theoretical_max > 0 else 0
            )
            loss_percent = _shadow_loss_percent(actual_kwh, theoretical_max)
            return (
                (
                    {
                        "method": "sensor_fusion",
                        "mode": "solar_radiation_sensor_frost",
                        "shadow_type": "frost_snow",
                        "shadow_percent": round(loss_percent, 1),
                        "confidence": 0.9,
                        "root_cause": "panel_frost_snow",
                        "interpretation": f"Possible frost/snow on panels (temp: {temperature:.1f}C, only {efficiency_ratio * 100:.0f}% efficiency)",
                    }
                    if True
                    else None
                )
                if True
                else None
            )
        if clear_sky_radiation < self.MIN_CLEAR_SKY_RADIATION_WM2:
            return (
                (
                    self._not_evaluable_fusion_result(
                        "low_radiation",
                        "solar_radiation_sensor_low_radiation",
                        "Clear-sky radiation too low for reliable correlation",
                    )
                    if True
                    else None
                )
                if True
                else None
            )
        irradiance_factor = sensor_radiation / clear_sky_radiation
        production_factor = actual_kwh / theoretical_max if theoretical_max > 0 else 0.0
        irradiance_drop = max(0.0, min(1.0, 1.0 - irradiance_factor))
        production_drop = max(0.0, min(1.0, 1.0 - production_factor))
        correlation_threshold = self.IRRADIANCE_CORRELATION_THRESHOLD_BASE
        if sun_elevation < 20:
            correlation_threshold += 0.08
        elif sun_elevation < 30:
            correlation_threshold += 0.04
        correlation_difference = production_drop - irradiance_drop
        if abs(correlation_difference) < correlation_threshold:
            root_cause = "weather_clouds"
            confidence = 0.94
            interpretation = f"Weather-correlated loss (Irradiance: -{irradiance_drop * 100:.0f}%, Prod: -{production_drop * 100:.0f}%)"
            possible_causes = ["Cloud cover", "Atmospheric haze"]
        elif correlation_difference > correlation_threshold:
            root_cause = "building_tree_obstruction"
            confidence = 0.92
            interpretation = f"Local panel loss (Irradiance: -{irradiance_drop * 100:.0f}%, Prod: -{production_drop * 100:.0f}%)"
            possible_causes = [
                "Building shadow",
                "Tree obstruction",
                "Horizon blocking",
                "Panel soiling",
            ]
        else:
            root_cause = "weather_better_than_forecast"
            confidence = 0.78
            interpretation = f"Pyranometer loss exceeds panel loss (Irradiance: -{irradiance_drop * 100:.0f}%, Prod: -{production_drop * 100:.0f}%)"
            possible_causes = [
                "Local pyranometer obstruction",
                "Panel orientation advantage",
                "Sensor calibration",
            ]
        loss_percent = production_drop * 100.0
        if loss_percent < self.SHADOW_THRESHOLD_LIGHT * 100:
            shadow_type = "none"
        elif loss_percent < self.SHADOW_THRESHOLD_MODERATE * 100:
            shadow_type = "light"
        elif loss_percent < self.SHADOW_THRESHOLD_HEAVY * 100:
            shadow_type = "moderate"
        else:
            shadow_type = "heavy"
        return (
            (
                {
                    "method": "sensor_fusion",
                    "mode": "solar_radiation_sensor",
                    "shadow_type": shadow_type,
                    "shadow_percent": round(loss_percent, 1),
                    "confidence": confidence,
                    "interpretation": interpretation,
                    "root_cause": root_cause,
                    "possible_causes": possible_causes,
                    "efficiency_ratio": round(production_factor, 3),
                    "loss_kwh": round(max(0.0, theoretical_max - actual_kwh), 4),
                    "irradiance_factor": round(irradiance_factor, 4),
                    "irradiance_shadow_percent": round(irradiance_drop * 100.0, 1),
                    "irradiance_analysis": {
                        "sensor_radiation_wm2": round(sensor_radiation, 1),
                        "clear_sky_radiation_wm2": round(clear_sky_radiation, 1),
                        "irradiance_drop_percent": round(irradiance_drop * 100.0, 1),
                        "production_drop_percent": round(production_drop * 100.0, 1),
                        "correlation_diff_percent": round(
                            correlation_difference * 100.0, 1
                        ),
                        "threshold_used_percent": round(
                            correlation_threshold * 100.0, 1
                        ),
                        "sun_elevation_deg": round(sun_elevation, 1),
                        "cloud_cover_percent": (
                            round(cloud_cover, 1) if cloud_cover is not None else None
                        ),
                    },
                }
                if True
                else None
            )
            if True
            else None
        )

    async def _detect_with_lux_correlation(
        self,
        prediction: Dict[str, Any],
        cloud_cover: float,
        clear_sky_rad: float,
        sensor_lux: float,
        actual_kwh: float,
        theoretical_max: float,
        sun_elevation: float,
        temperature: float,
    ) -> Dict[str, Any]:
        """Correlation-based shadow detection WITH Lux sensor. @zara

        Key insight: The Lux sensor is usually NOT at the solar panels!

        Logic:
        - If Lux drops AND Production drops by similar amounts -> CLOUDS
        - If Production drops MORE than Lux -> PANEL SHADOW (building/tree)
        - If Lux drops MORE than Production -> SENSOR SHADOW (rare)
        """
        if sun_elevation < 10:
            return self._not_evaluable_fusion_result(
                "low_sun_angle",
                "lux_sensor_low_angle",
                f"Sun elevation too low ({sun_elevation:.1f}deg) for reliable detection",
            )
        if temperature < 0 and actual_kwh < theoretical_max * 0.15:
            efficiency_ratio = (
                actual_kwh / theoretical_max if theoretical_max > 0 else 0
            )
            loss_percent = _shadow_loss_percent(actual_kwh, theoretical_max)
            return {
                "method": "sensor_fusion",
                "mode": "lux_sensor_frost",
                "shadow_type": "frost_snow",
                "shadow_percent": round(loss_percent, 1),
                "confidence": 0.85,
                "root_cause": "panel_frost_snow",
                "interpretation": f"Possible frost/snow on panels (temp: {temperature:.1f}C, only {efficiency_ratio * 100:.0f}% efficiency)",
                "lux_analysis": {
                    "sensor_lux": round(sensor_lux, 1),
                    "temperature_c": round(temperature, 1),
                },
            }
        clear_sky_lux = clear_sky_rad * 75.0
        if clear_sky_lux < 100:
            return self._not_evaluable_fusion_result(
                "low_radiation",
                "lux_sensor_low_radiation",
                "Clear-sky radiation too low for meaningful analysis",
            )
        lux_drop = 1.0 - sensor_lux / clear_sky_lux
        production_drop = 1.0 - actual_kwh / theoretical_max
        lux_drop = max(0.0, min(1.0, lux_drop))
        production_drop = max(0.0, min(1.0, production_drop))
        correlation_threshold = self.CORRELATION_THRESHOLD_BASE
        if cloud_cover > 80:
            correlation_threshold -= 0.05
        elif cloud_cover > 60:
            correlation_threshold -= 0.02
        correlation_threshold = max(correlation_threshold, 0.08)
        if sun_elevation < 20:
            correlation_threshold += 0.1
        elif sun_elevation < 30:
            correlation_threshold += 0.05
        correlation_threshold = min(correlation_threshold, 0.35)
        correlation_difference = production_drop - lux_drop
        if abs(correlation_difference) < correlation_threshold:
            root_cause = "weather_clouds"
            confidence = 0.88
            interpretation = f"Cloud-based shading (Lux: -{lux_drop * 100:.0f}%, Prod: -{production_drop * 100:.0f}% - correlated)"
            possible_causes = ["Cloud cover", "Overcast sky", "Atmospheric haze"]
        elif correlation_difference > correlation_threshold:
            root_cause = "building_tree_obstruction"
            confidence = 0.82
            interpretation = f"Local panel shadow (Lux: -{lux_drop * 100:.0f}%, Prod: -{production_drop * 100:.0f}% - uncorrelated)"
            possible_causes = [
                "Building shadow",
                "Tree obstruction",
                "Horizon blocking",
                "Panel soiling",
            ]
        else:
            root_cause = "weather_better_than_forecast"
            confidence = 0.7
            interpretation = f"Lux sensor affected more than panels (Lux: -{lux_drop * 100:.0f}%, Prod: -{production_drop * 100:.0f}%)"
            possible_causes = [
                "Lux sensor in partial shadow",
                "Panels in better position",
            ]
        loss_percent = production_drop * 100.0
        if loss_percent < self.SHADOW_THRESHOLD_LIGHT * 100:
            shadow_type = "none"
        elif loss_percent < self.SHADOW_THRESHOLD_MODERATE * 100:
            shadow_type = "light"
        elif loss_percent < self.SHADOW_THRESHOLD_HEAVY * 100:
            shadow_type = "moderate"
        else:
            shadow_type = "heavy"
        return {
            "method": "sensor_fusion",
            "mode": "lux_sensor",
            "shadow_type": shadow_type,
            "shadow_percent": round(loss_percent, 1),
            "confidence": confidence,
            "interpretation": interpretation,
            "root_cause": root_cause,
            "possible_causes": possible_causes,
            "efficiency_ratio": round(actual_kwh / theoretical_max, 3),
            "loss_kwh": round(max(0.0, theoretical_max - actual_kwh), 4),
            "lux_analysis": {
                "factor": round(sensor_lux / clear_sky_lux, 4),
                "shadow_percent": round(lux_drop * 100, 1),
                "sensor_lux": round(sensor_lux, 1),
                "clear_sky_lux": round(clear_sky_lux, 1),
                "lux_drop_percent": round(lux_drop * 100, 1),
                "production_drop_percent": round(production_drop * 100, 1),
                "correlation_diff_percent": round(correlation_difference * 100, 1),
                "threshold_used_percent": round(correlation_threshold * 100, 1),
                "sun_elevation_deg": round(sun_elevation, 1),
                "cloud_cover_percent": round(cloud_cover, 1),
            },
        }

    async def _detect_with_cloud_cover(
        self,
        prediction: Dict[str, Any],
        cloud_cover: Optional[float],
        actual_kwh: float,
        theoretical_max: float,
    ) -> Dict[str, Any]:
        """Fallback detection WITHOUT Lux/radiation sensors."""
        precipitation = self._first_numeric(
            (prediction.get("weather_actual") or {}).get("precipitation_mm"),
            (prediction.get("weather_corrected") or {}).get("precipitation_mm"),
            (prediction.get("weather_forecast") or {}).get("precipitation_mm"),
        )
        observation = classify_sensorless_hour(
            cloud_cover=cloud_cover,
            precipitation_mm=precipitation,
            sun_elevation_deg=self._first_numeric(
                (prediction.get("astronomy") or {}).get("sun_elevation_deg"),
                (prediction.get("astronomy") or {}).get("elevation_deg"),
            ),
            actual_kwh=actual_kwh,
            theoretical_max_kwh=theoretical_max,
            ceiling_ready=False,
        )
        if not observation["evaluable"]:
            return self._sensorless_not_evaluable_result(
                (
                    "weather_not_clear_or_reliable"
                    if observation["root_cause"] == "weather_clouds"
                    else "missing_weather_reference"
                ),
                observation["clear_sky_loss_percent"],
                observation["interpretation"],
                cloud_cover=cloud_cover,
                weather_loss_percent=observation.get("weather_loss_percent"),
                precipitation=precipitation,
            )
        return {
            "method": "sensor_fusion",
            "mode": observation["mode"],
            "shadow_type": observation["shadow_type"],
            "shadow_percent": observation["shadow_percent"],
            "confidence": observation["confidence"],
            "interpretation": observation["interpretation"],
            "root_cause": observation["root_cause"],
            "possible_causes": [],
            "evaluable": True,
            "learning_eligible": True,
            "observation_class": observation["observation_class"],
            "clear_sky_loss_percent": observation["clear_sky_loss_percent"],
            "local_shadow_percent": observation["local_shadow_percent"],
            "weather_loss_percent": observation["weather_loss_percent"],
            "cloud_analysis": {
                "cloud_cover_percent": cloud_cover,
                "expected_kwh": None,
                "actual_kwh": actual_kwh,
                "deviation_percent": 0.0,
                "precipitation_mm": precipitation,
            },
        }

    def _sensorless_not_evaluable_result(
        self,
        reason: str,
        clear_sky_loss_percent: float,
        interpretation: str,
        *,
        cloud_cover: Optional[float] = None,
        expected_kwh: Optional[float] = None,
        weather_loss_percent: Optional[float] = None,
        precipitation: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Keep weather loss observable without treating it as local shadow."""
        result = self._not_evaluable_fusion_result(
            reason, "cloud_based", interpretation
        )
        result.update(
            {
                "clear_sky_loss_percent": round(clear_sky_loss_percent, 1),
                "local_shadow_percent": 0.0,
                "weather_loss_percent": (
                    round(weather_loss_percent, 1)
                    if weather_loss_percent is not None
                    else None
                ),
                "root_cause": (
                    "weather_clouds"
                    if cloud_cover is not None
                    else "insufficient_reference"
                ),
                "observation_class": OBSERVATION_NOT_EVALUABLE,
                "cloud_analysis": {
                    "cloud_cover_percent": cloud_cover,
                    "expected_kwh": (
                        round(expected_kwh, 3) if expected_kwh is not None else None
                    ),
                    "precipitation_mm": precipitation,
                },
            }
        )
        return result

    async def detect_shadow_ensemble(
        self, prediction: Dict[str, Any]
    ) -> Dict[str, Any]:
        "Ensemble Detection - Combines both methods for best results. @zara\n\nWeighted combination:\n- Theory Ratio: 60% (reliable baseline)\n- Sensor Fusion: 40% (adds weather/lux context)\n\nArgs:\n    prediction: Hourly prediction dict\n\nReturns:\n    Combined shadow detection result with all method outputs\n"
        try:
            await self._ensure_pattern_learner()
            theory_result = await self.detect_shadow_theory_ratio(prediction)
            fusion_result = await self.detect_shadow_sensor_fusion(prediction)
            if theory_result.get("shadow_type") == "error":
                _LOGGER.warning("Theory ratio failed, using sensor fusion only")
                return {
                    **fusion_result,
                    "ensemble_mode": "fusion_only",
                    "methods": {
                        "theory_ratio": theory_result,
                        "sensor_fusion": fusion_result,
                    },
                }
            if fusion_result.get("shadow_type") == "error":
                _LOGGER.warning("Sensor fusion failed, using theory ratio only")
                theory_only_result = {
                    **theory_result,
                    "ensemble_mode": "theory_only",
                    "root_cause": theory_result.get("root_cause") or "unknown",
                    "evaluable": False,
                    "learning_eligible": False,
                    "observation_class": OBSERVATION_NOT_EVALUABLE,
                    "methods": {
                        "theory_ratio": theory_result,
                        "sensor_fusion": fusion_result,
                    },
                }
                return self._apply_learned_patterns(theory_only_result, prediction)
            if theory_result.get("shadow_type") == "night":
                return {
                    **theory_result,
                    "ensemble_mode": "night",
                    "root_cause": "night",
                    "methods": {
                        "theory_ratio": theory_result,
                        "sensor_fusion": fusion_result,
                    },
                }
            if not fusion_result.get("evaluable", True):
                if fusion_result.get("mode") != "cloud_based":
                    return {
                        "method": "ensemble",
                        "ensemble_mode": "not_evaluable",
                        "shadow_type": "not_evaluable",
                        "shadow_percent": 0.0,
                        "confidence": 0.0,
                        "root_cause": "insufficient_reference",
                        "fusion_mode": fusion_result.get("mode", "unknown"),
                        "interpretation": fusion_result.get("interpretation"),
                        "evaluable": False,
                        "learning_eligible": False,
                        "not_evaluable_reason": fusion_result.get(
                            "not_evaluable_reason"
                        ),
                        "methods": {
                            "theory_ratio": theory_result,
                            "sensor_fusion": fusion_result,
                        },
                        "weights": {"theory_ratio": 0.0, "sensor_fusion": 0.0},
                    }
            if fusion_result.get("mode") == "cloud_based":
                cloud_result = {
                    "method": "ensemble",
                    "ensemble_mode": "cloud_based",
                    "shadow_type": fusion_result.get("shadow_type", "not_evaluable"),
                    "shadow_percent": fusion_result.get("local_shadow_percent", 0.0),
                    "confidence": fusion_result.get("confidence", 0.0),
                    "root_cause": fusion_result.get(
                        "root_cause", "insufficient_reference"
                    ),
                    "fusion_mode": "cloud_based",
                    "evaluable": fusion_result.get("evaluable", False),
                    "learning_eligible": fusion_result.get("learning_eligible", False),
                    "observation_class": fusion_result.get(
                        "observation_class", OBSERVATION_NOT_EVALUABLE
                    ),
                    "clear_sky_loss_percent": fusion_result.get(
                        "clear_sky_loss_percent"
                    ),
                    "local_shadow_percent": fusion_result.get(
                        "local_shadow_percent", 0.0
                    ),
                    "weather_loss_percent": fusion_result.get("weather_loss_percent"),
                    "interpretation": fusion_result.get("interpretation"),
                    "methods": {
                        "theory_ratio": theory_result,
                        "sensor_fusion": fusion_result,
                    },
                    "weights": {
                        "theory_ratio": 0.0,
                        "sensor_fusion": (
                            1.0 if fusion_result.get("evaluable", False) else 0.0
                        ),
                    },
                }
                return self._apply_learned_patterns(cloud_result, prediction)
            theory_shadow_percent = theory_result.get("shadow_percent", 0)
            fusion_shadow_percent = fusion_result.get("shadow_percent", 0)
            ensemble_shadow_percent = (
                theory_shadow_percent * self.WEIGHT_THEORY
                + fusion_shadow_percent * self.WEIGHT_SENSOR
            )
            theory_confidence = theory_result.get("confidence", 0)
            fusion_confidence = fusion_result.get("confidence", 0)
            ensemble_confidence = (
                theory_confidence * self.WEIGHT_THEORY
                + fusion_confidence * self.WEIGHT_SENSOR
            )
            if ensemble_shadow_percent < self.SHADOW_THRESHOLD_LIGHT * 100:
                ensemble_shadow_type = "none"
            elif ensemble_shadow_percent < self.SHADOW_THRESHOLD_MODERATE * 100:
                ensemble_shadow_type = "light"
            elif ensemble_shadow_percent < self.SHADOW_THRESHOLD_HEAVY * 100:
                ensemble_shadow_type = "moderate"
            else:
                ensemble_shadow_type = "heavy"
            root_cause = fusion_result.get("root_cause")
            if not root_cause or root_cause == "unknown":
                possible_causes = theory_result.get("possible_causes", [])
                if possible_causes and ensemble_shadow_type in ("moderate", "heavy"):
                    root_cause = "possible_obstruction"
                elif ensemble_shadow_type == "none":
                    root_cause = "normal_variation"
                else:
                    root_cause = "unknown"
            fusion_mode = fusion_result.get("mode", "unknown")
            result = {
                "method": "ensemble",
                "ensemble_mode": "full",
                "shadow_type": ensemble_shadow_type,
                "shadow_percent": round(ensemble_shadow_percent, 1),
                "confidence": round(ensemble_confidence, 2),
                "root_cause": root_cause,
                "fusion_mode": fusion_mode,
                "evaluable": True,
                "learning_eligible": True,
                "observation_class": classify_observation_class(
                    fusion_mode=fusion_mode,
                    ensemble_mode="full",
                    root_cause=root_cause,
                    evaluable=True,
                    shadow_candidate=ensemble_shadow_type in ("moderate", "heavy")
                    and root_cause
                    in {"possible_obstruction", "building_tree_obstruction"},
                ),
                "efficiency_ratio": theory_result.get("efficiency_ratio", 0),
                "loss_kwh": theory_result.get("loss_kwh", 0),
                "theoretical_max_kwh": theory_result.get("theoretical_max_kwh", 0),
                "interpretation": self._generate_ensemble_interpretation(
                    ensemble_shadow_type,
                    ensemble_shadow_percent,
                    root_cause,
                    fusion_result,
                ),
                "methods": {
                    "theory_ratio": theory_result,
                    "sensor_fusion": fusion_result,
                },
                "weights": {
                    "theory_ratio": self.WEIGHT_THEORY,
                    "sensor_fusion": self.WEIGHT_SENSOR,
                },
            }
            actual_weather = prediction.get("weather_actual") or {}
            if actual_weather.get("snow_covered_panels"):
                result["root_cause"] = "snow_frost"
                result["snow_detected"] = True
                result["interpretation"] = (
                    f"Snow on panels detected - {ensemble_shadow_percent:.1f}% production loss"
                )
                _LOGGER.debug(
                    "Shadow detection: Snow flag detected, overriding root_cause"
                )
            elif actual_weather.get("frost_detected") in (
                "heavy_frost",
                "light_frost",
                "possible_frost",
            ) and ensemble_shadow_type in ("moderate", "heavy"):
                frost_type = actual_weather.get("frost_detected")
                result["root_cause"] = "panel_frost"
                result["frost_detected"] = True
                result["interpretation"] = (
                    f"Frost on panels ({frost_type}) - {ensemble_shadow_percent:.1f}% production loss"
                )
                _LOGGER.debug(
                    "Shadow detection: Frost flag '%s' detected, overriding root_cause",
                    frost_type,
                )
            result = self._apply_learned_patterns(result, prediction)
            return result
        except Exception as error:
            _LOGGER.error("Ensemble shadow detection failed: %s", error, exc_info=True)
            return {
                "method": "ensemble",
                "error": str(error),
                "shadow_type": "error",
                "confidence": 0.0,
            }

    def _generate_ensemble_interpretation(
        self,
        shadow_type: str,
        shadow_percent: float,
        root_cause: str,
        fusion_result: Dict[str, Any],
    ) -> str:
        """Generate human-readable interpretation of ensemble result. @zara"""
        if shadow_type == "none":
            loss_description = (
                f"Excellent production ({100 - shadow_percent:.0f}% of theoretical max)"
            )
        elif shadow_type == "light":
            loss_description = f"Light shadowing ({shadow_percent:.0f}% loss)"
        elif shadow_type == "moderate":
            loss_description = f"Moderate shadowing ({shadow_percent:.0f}% loss)"
        else:
            loss_description = f"Heavy shadowing ({shadow_percent:.0f}% loss)"
        if root_cause == "weather_clouds":
            cause_description = "primarily due to cloud cover"
        elif root_cause == "building_tree_obstruction":
            cause_description = "likely due to local obstruction (building/tree)"
        elif root_cause == "normal_variation":
            cause_description = "within normal operational range"
        else:
            cause_description = "cause unclear, monitor system"
        return f"{loss_description} - {cause_description}"

    def _apply_learned_patterns(
        self, result: Dict[str, Any], prediction: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Apply learned shadow patterns to enhance detection result. @zara V16.2

        Uses the ShadowPatternLearner to:
        - Boost confidence if current detection matches learned pattern
        - Add pattern context (is this a known fixed obstruction hour?)
        - Adjust root cause based on historical data

        Args:
            result: Current detection result dict
            prediction: Original prediction dict with hour info

        Returns:
            Enhanced result dict with pattern information
        """
        if not self._pattern_learner or not self._pattern_learner._initialized:
            return result
        try:
            astronomy = prediction.get("astronomy") or {}
            hour = prediction.get("target_hour")
            if hour is None:
                hour = astronomy.get("hour")
            if hour is None:
                target_datetime = prediction.get("target_datetime")
                if target_datetime:
                    try:
                        if isinstance(target_datetime, str):
                            hour = int(target_datetime.split("T")[1].split(":")[0])
                        elif hasattr(target_datetime, "hour"):
                            hour = target_datetime.hour
                    except (IndexError, ValueError, AttributeError):
                        pass
            if hour is None:
                prediction_id = str(prediction.get("prediction_id") or "")
                if "_" in prediction_id:
                    hour_suffix = prediction_id.rsplit("_", 1)[-1]
                    if hour_suffix.isdigit():
                        hour = int(hour_suffix)
            if hour is None:
                return result
            month = SafeDateTimeUtil.now().month
            pattern = self._pattern_learner.get_expected_shadow(hour=hour, month=month)
            if not pattern.get("has_pattern"):
                result["learned_pattern"] = {
                    "available": False,
                    "message": "Pattern not yet learned for this hour",
                }
                return result
            result["learned_pattern"] = {
                "available": True,
                "pattern_type": pattern.get("pattern_type"),
                "expected_shadow": pattern.get("expected_shadow"),
                "expected_shadow_percent": pattern.get("expected_shadow_percent"),
                "is_fixed_obstruction": pattern.get("is_fixed_obstruction"),
                "pattern_confidence": pattern.get("confidence"),
                "dominant_cause": pattern.get("dominant_cause"),
            }
            observed_shadow = result.get("shadow_percent", 0)
            expected_shadow = pattern.get("expected_shadow_percent", 0)
            shadow_type = result.get("shadow_type", "unknown")
            sensorless = (
                result.get("fusion_mode") == "cloud_based"
                or result.get("ensemble_mode") == "cloud_based"
            )
            shadow_difference = abs(observed_shadow - expected_shadow)
            pattern_matches = shadow_difference < 15
            if not sensorless:
                if pattern_matches and pattern.get("confidence", 0) > 0.5:
                    confidence_bonus = min(0.1, pattern.get("confidence", 0) * 0.15)
                    previous_confidence = result.get("confidence", 0)
                    result["confidence"] = min(
                        1.0, previous_confidence + confidence_bonus
                    )
                    result["learned_pattern"]["confidence_boosted"] = True
                    result["learned_pattern"]["confidence_boost"] = round(
                        confidence_bonus, 3
                    )
            current_cause = result.get("root_cause", "")
            protected_causes = ["snow_frost", "panel_frost", "panel_frost_snow"]
            if (
                pattern.get("is_fixed_obstruction")
                and (not sensorless)
                and (shadow_type in ("moderate", "heavy"))
            ):
                if current_cause not in protected_causes:
                    result["root_cause"] = "building_tree_obstruction"
                    result["learned_pattern"]["root_cause_from_pattern"] = True
                    result["interpretation"] = (
                        f"{result.get('interpretation', '')} (known fixed obstruction at this hour)".strip()
                    )
                else:
                    result["learned_pattern"]["pattern_overridden_by"] = current_cause
            if not pattern.get("expected_shadow"):
                if (
                    shadow_type in ("moderate", "heavy")
                    and pattern.get("confidence", 0) > 0.6
                ):
                    result["learned_pattern"]["unusual_shadow"] = True
                    result["learned_pattern"][
                        "unusual_message"
                    ] = f"Shadow detected but not expected based on {pattern.get('confidence', 0):.0%} confident pattern"
            return result
        except Exception as error:
            _LOGGER.warning("Failed to apply learned patterns: %s", error)
            result["learned_pattern"] = {"available": False, "error": str(error)}
            return result

    async def detect_shadow_per_group(
        self,
        prediction: Dict[str, Any],
        panel_group_actuals: Dict[str, Dict[str, Any]],
        system_ensemble: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        "Per-group shadow detection using real per-group actual_kwh. @zara V17.0.0\n\nKey difference from _analyze_per_group_shadows(): Uses ACTUAL per-group\nproduction data instead of estimating from system-wide efficiency.\n\nArgs:\n    prediction: Hourly prediction dict with astronomy data\n    panel_group_actuals: Dict mapping group_name → {actual_kwh, theoretical_max_kwh, ...}\n    system_ensemble: Optional system-wide ensemble result for sensor fusion weighting\n\nReturns:\n    Dict mapping group_name → per-group shadow detection result\n"
        group_comparison = self._compare_panel_group_performance(
            prediction, panel_group_actuals, system_ensemble
        )
        prediction_id = prediction.get("prediction_id", "")
        if not prediction_id:
            return group_comparison
        try:
            async with self.db.transaction():
                for group_name, group_result in group_comparison.items():
                    await self.db.save_shadow_detection_group(
                        prediction_id, group_name, group_result, auto_commit=False
                    )
        except Exception as write_error:
            _LOGGER.error(
                "Per-group shadow detection failed: %s", write_error, exc_info=True
            )
        return group_comparison if True else None

    def _compare_panel_group_performance(
        self,
        prediction: Dict[str, Any],
        panel_group_actuals: Dict[str, Dict[str, Any]],
        system_ensemble: Optional[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """Compare complete groups only; expected kWh normalizes capacity and topology."""
        flags = prediction.get("flags") or {}
        fusion = (system_ensemble or {}).get("methods", {}).get("sensor_fusion", {})
        reference_valid = (
            bool((system_ensemble or {}).get("evaluable", True))
            and fusion.get("mode") == "solar_radiation_sensor"
            and fusion.get("evaluable", True)
            and (not shadow_flags_mark_production_limited(flags))
        )
        return compare_panel_group_performance(
            panel_group_actuals, reference_is_valid=reference_valid
        )


class PerformanceLossAnalyzer(DataManagerIO):
    """Analyzes performance losses due to shading over time. @zara

    Provides daily summaries, trends, and pattern detection.
    Uses database for persistence.
    """

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        """Initialize performance loss analyzer. @zara"""
        super().__init__(hass, db_manager)
        _LOGGER.info("PerformanceLossAnalyzer initialized with DatabaseManager")

    async def _table_has_column(self, table: str, column: str) -> bool:
        """Return False when the table or column cannot be read."""
        try:
            rows = await self.fetch_all(f"PRAGMA table_info({table})")
        except Exception:
            return False
        for row in rows or []:
            column_name = row["name"] if isinstance(row, dict) else row[1]
            if column_name == column:
                return True
        return False

    async def analyze_daily_shadow(self, date_str: str) -> Dict[str, Any]:
        "Analyze shadow performance for a specific day from database. @zara\n\nArgs:\n    date_str: Date string (YYYY-MM-DD)\n\nReturns:\n    Daily shadow analysis summary\n"
        try:
            curtailment_filter = ""
            if await self._table_has_column(
                "hourly_predictions", "mppt_throttle_reason"
            ):
                curtailment_filter = f" AND COALESCE(hp.mppt_throttle_reason, '') NOT IN ({CURTAILMENT_THROTTLE_REASON_SQL})"
            rows = await self.fetch_all(
                f"SELECT hp.target_hour, hp.prediction_kwh, hp.actual_kwh,\n                          pa.theoretical_max_kwh\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_astronomy pa\n                       ON hp.prediction_id = pa.prediction_id\n                   INNER JOIN hourly_shadow_detection hsd\n                       ON hp.prediction_id = hsd.prediction_id\n                   WHERE hp.target_date = ?\n                     AND hp.actual_kwh IS NOT NULL\n                     AND pa.theoretical_max_kwh > 0.01\n                     AND COALESCE(hp.mppt_throttled, 0) = 0\n                     AND COALESCE(hp.inverter_clipped, 0) = 0\n                     AND COALESCE(hp.has_weather_alert, 0) = 0\n                     {curtailment_filter}\n                     AND hsd.shadow_type NOT IN ('not_evaluable', 'error', 'unknown')\n                     AND hsd.root_cause IN (\n                         'building_tree_obstruction', 'possible_obstruction'\n                     )\n                     AND (\n                         COALESCE(hp.has_panel_group_actuals, 0) = 1\n                         OR NOT EXISTS (\n                             SELECT 1 FROM prediction_panel_groups ppg\n                             WHERE ppg.prediction_id = hp.prediction_id\n                         )\n                     )\n                   ORDER BY hp.target_hour",
                (date_str,),
            )
            if not rows:
                return {
                    "date": date_str,
                    "error": "No prediction data with actuals available",
                    "total_hours_analyzed": 0,
                }
            shadow_hours = []
            cumulative_loss = 0.0
            cumulative_theoretical = 0.0
            peak_shadow_hour = None
            peak_shadow_percent = 0.0
            for row in rows:
                hour = row[0]
                actual_kwh = row[2] or 0
                theoretical_max_kwh = row[3] or 0
                if theoretical_max_kwh <= 0:
                    continue
                loss_percent = _shadow_loss_percent(actual_kwh, theoretical_max_kwh)
                loss_kwh = theoretical_max_kwh - actual_kwh
                cumulative_loss += loss_kwh
                cumulative_theoretical += theoretical_max_kwh
                if loss_percent > peak_shadow_percent:
                    peak_shadow_percent = loss_percent
                    peak_shadow_hour = hour
                if loss_percent > 15:
                    shadow_hours.append(
                        {
                            "hour": hour,
                            "shadow_percent": round(loss_percent, 1),
                            "loss_kwh": round(loss_kwh, 4),
                        }
                    )
            daily_loss_percent = (
                cumulative_loss / cumulative_theoretical * 100.0
                if cumulative_theoretical > 0
                else 0.0
            )
            return {
                "date": date_str,
                "total_hours_analyzed": len(rows),
                "shadow_hours_count": len(shadow_hours),
                "shadow_hours": [shadow_hour["hour"] for shadow_hour in shadow_hours],
                "peak_shadow_hour": peak_shadow_hour,
                "peak_shadow_percent": round(peak_shadow_percent, 1),
                "cumulative_loss_kwh": round(cumulative_loss, 3),
                "cumulative_theoretical_kwh": round(cumulative_theoretical, 3),
                "daily_loss_percent": round(daily_loss_percent, 1),
                "interpretation": self._interpret_daily_shadow(
                    len(shadow_hours), daily_loss_percent
                ),
            }
            pass
        except Exception as error:
            _LOGGER.error("Daily shadow analysis failed: %s", error, exc_info=True)
            return {"date": date_str, "error": str(error), "total_hours_analyzed": 0}

    async def analyze_sensorless_observation_persistence(
        self, *, as_of_date: str, window_days: int = CEILING_WINDOW_DAYS
    ) -> Dict[str, Any]:
        """Replay sensorless observation against stored actuals without promotion."""
        end_date = date.fromisoformat(as_of_date)
        window_length = max(1, int(window_days))
        start_date = end_date - timedelta(days=window_length - 1)
        rows = await self.fetch_all(
            "\n            SELECT hp.target_date,\n                   hp.target_hour,\n                   hp.actual_kwh,\n                   pa.theoretical_max_kwh,\n                   pa.sun_elevation_deg,\n                   pa.sun_azimuth_deg,\n                   COALESCE(\n                       (SELECT pw.clouds\n                          FROM prediction_weather pw\n                         WHERE pw.prediction_id = hp.prediction_id\n                           AND pw.weather_type = 'actual'\n                         ORDER BY pw.id DESC LIMIT 1),\n                       (SELECT wf.clouds\n                          FROM weather_forecast wf\n                         WHERE wf.forecast_date = hp.target_date\n                           AND wf.hour = hp.target_hour\n                         LIMIT 1)\n                   ) AS cloud_cover,\n                   COALESCE(\n                       (SELECT pw.rain\n                          FROM prediction_weather pw\n                         WHERE pw.prediction_id = hp.prediction_id\n                           AND pw.weather_type = 'actual'\n                         ORDER BY pw.id DESC LIMIT 1),\n                       0.0\n                   ) AS precipitation_mm\n              FROM hourly_predictions hp\n              LEFT JOIN prediction_astronomy pa\n                     ON pa.prediction_id = hp.prediction_id\n             WHERE hp.target_date BETWEEN ? AND ?\n               AND hp.actual_kwh IS NOT NULL\n               AND COALESCE(hp.exclude_from_learning, 0) = 0\n               AND COALESCE(hp.mppt_throttled, 0) = 0\n               AND COALESCE(hp.inverter_clipped, 0) = 0\n             ORDER BY hp.target_date, hp.target_hour\n            ",
            (start_date.isoformat(), end_date.isoformat()),
        )
        evaluable_observations = []
        for row in rows:
            observation = classify_sensorless_hour(
                cloud_cover=row[6],
                precipitation_mm=row[7],
                sun_elevation_deg=row[4],
                actual_kwh=row[2],
                theoretical_max_kwh=row[3],
                ceiling_ready=False,
            )
            if not observation["evaluable"]:
                continue
            evaluable_observations.append(
                {
                    "target_date": row[0],
                    "target_hour": row[1],
                    "actual_kwh": row[2],
                    "theoretical_max_kwh": row[3],
                    "sun_elevation_deg": row[4],
                    "sun_azimuth_deg": row[5],
                    "cloud_cover": row[6],
                    "observation_class": observation["observation_class"],
                    "clear_sky_loss_percent": observation["clear_sky_loss_percent"],
                    "elevation_bin": elevation_bin(row[4]),
                }
            )
        ceilings = build_geometry_ceilings(evaluable_observations)
        ready_bins = sum((1 for ceiling in ceilings.values() if ceiling["ready"]))
        return {
            "as_of_date": as_of_date,
            "window_days": window_length,
            "evaluable_hours": len(evaluable_observations),
            "source_hours": len(rows),
            "ready_bins": ready_bins,
            "bin_count": len(ceilings),
            "ceilings": {
                str(geometry_bin): ceiling
                for geometry_bin, ceiling in sorted(ceilings.items())
            },
            "promotion_eligible": False,
            "shadow_only": True,
        }

    async def analyze_group_peer_persistence(
        self,
        *,
        as_of_date: str,
        window_days: int = 30,
        group_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Measure repeatable peer anomalies without changing active classifications."""
        end_date = date.fromisoformat(as_of_date)
        window_length = max(1, int(window_days))
        start_date = end_date - timedelta(days=window_length - 1)
        query_params = [start_date.isoformat(), end_date.isoformat()]
        rows = await self.fetch_all(
            "\n            WITH eligible_predictions AS (\n                SELECT hp.prediction_id,\n                       hp.target_date,\n                       hp.target_hour,\n                       hp.prediction_created_at,\n                       hp.id AS prediction_row_id,\n                       hsd.fusion_mode,\n                       hsd.sensor_fusion_root_cause,\n                       hsd.sensor_fusion_confidence,\n                       COUNT(sdg.id) AS group_count,\n                       SUM(CASE\n                               WHEN sdg.actual_kwh IS NULL\n                                 OR sdg.actual_kwh < 0.0\n                                 OR sdg.theoretical_max_kwh IS NULL\n                                 OR sdg.theoretical_max_kwh < 0.01\n                               THEN 1 ELSE 0\n                           END) AS invalid_group_count\n                  FROM hourly_predictions hp\n                  JOIN hourly_shadow_detection hsd\n                    ON hsd.prediction_id = hp.prediction_id\n                  JOIN shadow_detection_groups sdg\n                    ON sdg.prediction_id = hp.prediction_id\n                 WHERE hp.target_date BETWEEN ? AND ?\n                   AND COALESCE(hp.has_panel_group_actuals, 0) = 1\n                   AND COALESCE(hp.exclude_from_learning, 0) = 0\n                   AND COALESCE(hp.mppt_throttled, 0) = 0\n                   AND COALESCE(hp.inverter_clipped, 0) = 0\n                   AND COALESCE(hp.weather_alert_type, '') = ''\n                   AND COALESCE(hp.actual_backfill_blocked, 0) = 0\n                   AND COALESCE(hp.data_quarantine_reason, '') = ''\n                   AND hsd.fusion_mode = 'solar_radiation_sensor'\n                   AND COALESCE(hsd.sensor_fusion_shadow_type, '')\n                       NOT IN ('not_evaluable', 'error')\n                 GROUP BY hp.prediction_id, hp.target_date, hp.target_hour,\n                          hp.prediction_created_at, hp.id, hsd.fusion_mode,\n                          hsd.sensor_fusion_root_cause,\n                          hsd.sensor_fusion_confidence\n                HAVING COUNT(sdg.id) >= 3\n                   AND SUM(CASE\n                               WHEN sdg.actual_kwh IS NULL\n                                 OR sdg.actual_kwh < 0.0\n                                 OR sdg.theoretical_max_kwh IS NULL\n                                 OR sdg.theoretical_max_kwh < 0.01\n                               THEN 1 ELSE 0\n                           END) = 0\n            ),\n            ranked_predictions AS (\n                SELECT eligible_predictions.*,\n                       ROW_NUMBER() OVER (\n                           PARTITION BY target_date, target_hour\n                           ORDER BY prediction_created_at DESC,\n                                    prediction_row_id DESC\n                       ) AS row_rank\n                  FROM eligible_predictions\n            )\n            SELECT ranked.prediction_id,\n                   ranked.target_date,\n                   ranked.target_hour,\n                   ranked.fusion_mode,\n                   ranked.sensor_fusion_root_cause,\n                   ranked.sensor_fusion_confidence,\n                   sdg.group_name,\n                   sdg.actual_kwh,\n                   sdg.theoretical_max_kwh\n              FROM ranked_predictions ranked\n              JOIN shadow_detection_groups sdg\n                ON sdg.prediction_id = ranked.prediction_id\n             WHERE ranked.row_rank = 1\n             ORDER BY ranked.target_date, ranked.target_hour, sdg.group_name\n            ",
            tuple(query_params),
        )
        rows_by_prediction = {}
        for row in rows:
            rows_by_prediction.setdefault(str(row[0]), []).append(row)
        observations_by_group = {}
        for group_rows in rows_by_prediction.values():
            first_row = group_rows[0]
            group_actuals = {
                str(row[6]): {"actual_kwh": row[7], "theoretical_max_kwh": row[8]}
                for row in group_rows
            }
            group_comparison = compare_panel_group_performance(
                group_actuals, reference_is_valid=True
            )
            for name, result in group_comparison.items():
                observations_by_group.setdefault(name, []).append(
                    {
                        "date": str(first_row[1]),
                        "hour": int(first_row[2]),
                        "peer_candidate": result.get("shadow_type")
                        == "possible_obstruction",
                        "peer_gap_percent": float(result.get("shadow_percent") or 0.0),
                        "fusion_mode": str(first_row[3] or "unknown"),
                        "fusion_root_cause": str(first_row[4] or "unknown"),
                        "fusion_confidence": float(first_row[5] or 0.0),
                    }
                )
            continue
        group_summaries = {
            name: self._summarize_group_peer_persistence(name, group_observations)
            for name, group_observations in sorted(observations_by_group.items())
            if group_name is None or name == group_name
        }
        return {
            "mode": "shadow_only",
            "window_start": start_date.isoformat(),
            "window_end": end_date.isoformat(),
            "window_days": window_length,
            "groups": group_summaries,
            "promotion_eligible": False,
        }

    @classmethod
    def _summarize_group_peer_persistence(
        cls, group_name: str, observations: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        candidate_observations = [row for row in observations if row["peer_candidate"]]
        evaluated_dates = {row["date"] for row in observations}
        candidate_dates = {row["date"] for row in candidate_observations}
        peer_gaps = [row["peer_gap_percent"] for row in candidate_observations]
        cause_counts = {}
        for row in candidate_observations:
            fusion_cause = row["fusion_root_cause"]
            cause_counts[fusion_cause] = cause_counts.get(fusion_cause, 0) + 1
        hourly_summary = {}
        for hour in sorted({row["hour"] for row in observations}):
            hour_observations = [row for row in observations if row["hour"] == hour]
            hour_candidates = [
                row for row in hour_observations if row["peer_candidate"]
            ]
            hour_evaluated_dates = {row["date"] for row in hour_observations}
            hour_candidate_dates = {row["date"] for row in hour_candidates}
            hour_peer_gaps = [row["peer_gap_percent"] for row in hour_candidates]
            hourly_summary[hour] = {
                "evaluated_observations": len(hour_observations),
                "peer_candidate_observations": len(hour_candidates),
                "peer_candidate_rate": (
                    round(len(hour_candidates) / len(hour_observations), 4)
                    if hour_observations
                    else 0.0
                ),
                "candidate_days": len(hour_candidate_dates),
                "evaluated_days": len(hour_evaluated_dates),
                "peer_candidate_day_rate": (
                    round(len(hour_candidate_dates) / len(hour_evaluated_dates), 4)
                    if hour_evaluated_dates
                    else 0.0
                ),
                "median_peer_gap_percent": (
                    round(median(hour_peer_gaps), 1) if hour_peer_gaps else 0.0
                ),
                "max_consecutive_candidate_days": cls._max_consecutive_dates(
                    hour_candidate_dates
                ),
            }
            continue
        sorted_candidate_dates = sorted(candidate_dates)
        return {
            "group_name": group_name,
            "evaluated_observations": len(observations),
            "evaluated_days": len(evaluated_dates),
            "peer_candidate_observations": len(candidate_observations),
            "peer_candidate_days": len(candidate_dates),
            "peer_candidate_rate": (
                round(len(candidate_observations) / len(observations), 4)
                if observations
                else 0.0
            ),
            "peer_candidate_day_rate": (
                round(len(candidate_dates) / len(evaluated_dates), 4)
                if evaluated_dates
                else 0.0
            ),
            "median_peer_gap_percent": (
                round(median(peer_gaps), 1) if peer_gaps else 0.0
            ),
            "max_peer_gap_percent": round(max(peer_gaps), 1) if peer_gaps else 0.0,
            "first_candidate_date": (
                sorted_candidate_dates[0] if sorted_candidate_dates else None
            ),
            "last_candidate_date": (
                sorted_candidate_dates[-1] if sorted_candidate_dates else None
            ),
            "max_consecutive_candidate_days": cls._max_consecutive_dates(
                candidate_dates
            ),
            "sample_ready": len(observations) >= 7,
            "fusion_cause_counts": dict(sorted(cause_counts.items())),
            "hourly": hourly_summary,
            "shadow_only": True,
            "promotion_eligible": False,
        }

    @staticmethod
    def _max_consecutive_dates(date_values: set[str]) -> int:
        sorted_dates = sorted(
            (date.fromisoformat(date_text) for date_text in date_values)
        )
        longest_run = 0
        current_run = 0
        previous_date = None
        for current_date in sorted_dates:
            current_run = (
                current_run + 1
                if previous_date and current_date == previous_date + timedelta(days=1)
                else 1
            )
            longest_run = max(longest_run, current_run)
            previous_date = current_date
        return longest_run

    def _interpret_daily_shadow(
        self, shadow_hours: int, daily_loss_percent: float
    ) -> str:
        """Generate interpretation of daily shadow analysis. @zara"""
        if shadow_hours == 0:
            return "Excellent day - no significant shadowing detected"
        if daily_loss_percent < 10:
            severity = "minimal"
        elif daily_loss_percent < 25:
            severity = "moderate"
        else:
            severity = "significant"
        return f"{severity.capitalize()} shadowing detected ({shadow_hours}h affected, {daily_loss_percent:.0f}% loss)"


_shadow_detector: Optional[ShadowDetector] = None
_performance_analyzer: Optional[PerformanceLossAnalyzer] = None


def get_shadow_detector(
    hass: HomeAssistant,
    db_manager: DatabaseManager,
    panel_groups: Optional[List[Dict[str, Any]]] = None,
) -> ShadowDetector:
    """Get or create shadow detector instance. @zara"""
    global _shadow_detector
    if _shadow_detector is None:
        _shadow_detector = ShadowDetector(
            hass=hass, db_manager=db_manager, panel_groups=panel_groups
        )
    elif panel_groups and (not _shadow_detector._use_panel_groups):
        _shadow_detector._panel_groups = panel_groups
        _shadow_detector._use_panel_groups = True
    return _shadow_detector if True else None


def get_performance_analyzer(
    hass: HomeAssistant, db_manager: DatabaseManager
) -> PerformanceLossAnalyzer:
    """Get or create performance analyzer instance. @zara"""
    global _performance_analyzer
    if _performance_analyzer is None:
        _performance_analyzer = PerformanceLossAnalyzer(hass, db_manager)
    return _performance_analyzer
