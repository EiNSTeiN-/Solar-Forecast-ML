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
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from .data_io import DataManagerIO

if TYPE_CHECKING:
    from .data_shadow_pattern_learning import ShadowPatternLearner
_LOGGER = logging.getLogger(__name__)


class ShadowDetector(DataManagerIO):
    """Detects and analyzes solar array shading using multiple methods. @zara

    V16.2.0 Methods:
    1. Theory Ratio - Compare actual vs theoretical_max (universal, fast)
    2. Correlation-Based Detection - Compare Lux drop vs Production drop (accurate)
    3. Ensemble - Weighted combination of both methods
    4. Pattern Learning - Uses learned patterns to improve confidence

    Uses correlation between Lux sensor and production to distinguish
    between cloud cover (affects both equally) and local shadows (only affects panels).
    """

    SHADOW_THRESHOLD_LIGHT = 0.05
    SHADOW_THRESHOLD_MODERATE = 0.15
    SHADOW_THRESHOLD_HEAVY = 0.3
    CORRELATION_THRESHOLD_BASE = 0.15
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
            _LOGGER.info(
                "ShadowDetector initialized with %d panel groups",
                len(self._panel_groups),
            )
        else:
            _LOGGER.info(
                "ShadowDetector initialized (V16.2.0: Pattern Learning Enabled)"
            )
        return None if True else None

    def set_pattern_learner(self, learner: "ShadowPatternLearner") -> None:
        "Set the pattern learner for learned pattern integration. @zara V16.2\n\nArgs:\n    learner: ShadowPatternLearner instance\n"
        self._pattern_learner = learner
        _LOGGER.debug("Shadow pattern learner connected to ShadowDetector")

    async def detect_shadow_theory_ratio(
        self, prediction: Dict[str, Any]
    ) -> Dict[str, Any]:
        "Method 1: Theory Ratio - Fast and Universal. @zara\n\nCompares actual production with theoretical maximum from clear sky model.\nWorks WITHOUT any external sensors (astronomy cache only).\n\nArgs:\n    prediction: Hourly prediction dict with actual_kwh and astronomy data\n\nReturns:\n    Shadow detection result with classification and confidence\n"
        try:
            actual_kwh = prediction.get("actual_kwh", 0)
            astronomy = prediction.get("astronomy", {})
            theoretical_max_kwh = astronomy.get("theoretical_max_kwh", 0)
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
            loss_fraction = max(0.0, 1.0 - efficiency_ratio)
            shadow_percent = loss_fraction * 100.0
            if loss_fraction < self.SHADOW_THRESHOLD_LIGHT:
                shadow_type = "none"
                confidence = 0.95
                interpretation = "Excellent production - minimal losses"
                possible_causes = []
            elif loss_fraction < self.SHADOW_THRESHOLD_MODERATE:
                shadow_type = "light"
                confidence = 0.85
                interpretation = "Light shadowing or atmospheric effects"
                possible_causes = [
                    "Thin clouds",
                    "Dust/pollen on panels",
                    "Morning/evening haze",
                ]
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
            loss_kwh = theoretical_max_kwh - actual_kwh
            result = {
                "method": "theory_ratio",
                "shadow_type": shadow_type,
                "shadow_percent": round(shadow_percent, 1),
                "confidence": confidence,
                "efficiency_ratio": round(efficiency_ratio, 3),
                "loss_kwh": round(loss_kwh, 3),
                "loss_percent": round(shadow_percent, 1),
                "interpretation": interpretation,
                "possible_causes": possible_causes,
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
        total_group_theory = sum(
            (panel_group.get("theoretical_kwh", 0) for panel_group in per_group_data)
        )
        if total_group_theory <= 0:
            return group_analysis
        system_efficiency_ratio = (
            actual_kwh / theoretical_max_total if theoretical_max_total > 0 else 0
        )
        for group_data in per_group_data:
            group_name = group_data.get("name", "Unknown")
            group_theoretical_kwh = group_data.get("theoretical_kwh", 0)
            tilt_deg = group_data.get("tilt_deg", 30)
            azimuth_deg = group_data.get("azimuth_deg", 180)
            group_actual_kwh = group_data.get("actual_kwh")
            if group_actual_kwh is not None and group_theoretical_kwh > 0:
                group_efficiency_ratio = group_actual_kwh / group_theoretical_kwh
                estimated_actual_kwh = group_actual_kwh
            else:
                group_efficiency_ratio = system_efficiency_ratio
                estimated_actual_kwh = group_theoretical_kwh * system_efficiency_ratio
            group_shadow_percent = (
                (1.0 - group_efficiency_ratio) * 100
                if group_efficiency_ratio < 1.0
                else 0
            )
            if group_shadow_percent < 5:
                shadow_type = "none"
                interpretation = "Normal production"
            elif group_shadow_percent < 15:
                shadow_type = "light"
                interpretation = "Light shading"
            elif group_shadow_percent < 30:
                shadow_type = "moderate"
                interpretation = "Moderate shading"
            else:
                shadow_type = "heavy"
                interpretation = "Heavy shading"
            group_analysis.append(
                {
                    "name": group_name,
                    "tilt_deg": tilt_deg,
                    "azimuth_deg": azimuth_deg,
                    "theoretical_kwh": round(group_theoretical_kwh, 4),
                    "estimated_actual_kwh": round(estimated_actual_kwh, 4),
                    "shadow_type": shadow_type,
                    "shadow_percent": round(group_shadow_percent, 1),
                    "interpretation": interpretation,
                }
            )
        return group_analysis

    async def detect_shadow_sensor_fusion(
        self, prediction: Dict[str, Any]
    ) -> Dict[str, Any]:
        "Method 2: Correlation-Based Detection - High Accuracy. @zara\n\nUses CORRELATION between Lux drop and Production drop to distinguish:\n- CLOUDS: Affect both Lux sensor AND panels equally (correlated drops)\n- LOCAL SHADOW: Only affects panels, not the Lux sensor (uncorrelated)\n\nKey insight: Lux sensor is usually NOT at the panels!\n\nArgs:\n    prediction: Hourly prediction dict with weather, astronomy, and sensor data\n\nReturns:\n    Shadow detection result with cloud/obstruction classification\n"
        try:
            weather = prediction.get("weather_corrected") or prediction.get(
                "weather_forecast", {}
            )
            astronomy = prediction.get("astronomy", {})
            sensor_actual = prediction.get("sensor_actual", {})
            cloud_cover = weather.get("clouds", weather.get("cloud_cover", 0))
            actual_kwh = prediction.get("actual_kwh", 0)
            theoretical_max_kwh = astronomy.get("theoretical_max_kwh", 0)
            clear_sky_radiation_wm2 = astronomy.get("clear_sky_radiation_wm2", 0)
            sun_elevation = astronomy.get(
                "sun_elevation_deg", astronomy.get("elevation_deg", 0)
            )
            temperature_c = weather.get("temperature", 15)
            if theoretical_max_kwh < 0.01:
                return {
                    "method": "sensor_fusion",
                    "shadow_type": "night",
                    "confidence": 1.0,
                    "mode": "night",
                }
            sensor_lux = sensor_actual.get("lux")
            has_lux = sensor_lux is not None and sensor_lux >= 0
            if has_lux:
                return await self._detect_with_lux_correlation(
                    prediction,
                    cloud_cover,
                    clear_sky_radiation_wm2,
                    sensor_lux,
                    actual_kwh,
                    theoretical_max_kwh,
                    sun_elevation,
                    temperature_c,
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
            return {
                "method": "sensor_fusion",
                "mode": "lux_sensor_low_angle",
                "shadow_type": "low_sun",
                "shadow_percent": 0.0,
                "confidence": 0.3,
                "root_cause": "low_sun_angle",
                "interpretation": f"Sun elevation too low ({sun_elevation:.1f}deg) for reliable detection",
                "lux_analysis": {
                    "sensor_lux": round(sensor_lux, 1),
                    "clear_sky_lux": round(clear_sky_rad * 75.0, 1),
                    "sun_elevation_deg": round(sun_elevation, 1),
                },
            }
        if temperature < 0 and actual_kwh < theoretical_max * 0.15:
            efficiency_ratio = (
                actual_kwh / theoretical_max if theoretical_max > 0 else 0
            )
            shadow_percent = max(0.0, (1.0 - efficiency_ratio) * 100.0)
            return {
                "method": "sensor_fusion",
                "mode": "lux_sensor_frost",
                "shadow_type": "frost_snow",
                "shadow_percent": round(shadow_percent, 1),
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
            return {
                "method": "sensor_fusion",
                "mode": "lux_sensor",
                "shadow_type": "low_radiation",
                "shadow_percent": 0.0,
                "confidence": 0.5,
                "root_cause": "low_radiation",
                "interpretation": "Clear-sky radiation too low for meaningful analysis",
            }
        lux_drop_fraction = 1.0 - sensor_lux / clear_sky_lux
        production_drop_fraction = 1.0 - actual_kwh / theoretical_max
        lux_drop_fraction = max(0.0, min(1.0, lux_drop_fraction))
        production_drop_fraction = max(0.0, min(1.0, production_drop_fraction))
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
        drop_difference = production_drop_fraction - lux_drop_fraction
        if abs(drop_difference) < correlation_threshold:
            root_cause = "weather_clouds"
            confidence = 0.88
            interpretation = f"Cloud-based shading (Lux: -{lux_drop_fraction * 100:.0f}%, Prod: -{production_drop_fraction * 100:.0f}% - correlated)"
            possible_causes = ["Cloud cover", "Overcast sky", "Atmospheric haze"]
        elif drop_difference > correlation_threshold:
            root_cause = "building_tree_obstruction"
            confidence = 0.82
            interpretation = f"Local panel shadow (Lux: -{lux_drop_fraction * 100:.0f}%, Prod: -{production_drop_fraction * 100:.0f}% - uncorrelated)"
            possible_causes = [
                "Building shadow",
                "Tree obstruction",
                "Horizon blocking",
                "Panel soiling",
            ]
        else:
            root_cause = "weather_better_than_forecast"
            confidence = 0.7
            interpretation = f"Lux sensor affected more than panels (Lux: -{lux_drop_fraction * 100:.0f}%, Prod: -{production_drop_fraction * 100:.0f}%)"
            possible_causes = [
                "Lux sensor in partial shadow",
                "Panels in better position",
            ]
        shadow_percent = production_drop_fraction * 100.0
        if shadow_percent < self.SHADOW_THRESHOLD_LIGHT * 100:
            shadow_type = "none"
        elif shadow_percent < self.SHADOW_THRESHOLD_MODERATE * 100:
            shadow_type = "light"
        elif shadow_percent < self.SHADOW_THRESHOLD_HEAVY * 100:
            shadow_type = "moderate"
        else:
            shadow_type = "heavy"
        return {
            "method": "sensor_fusion",
            "mode": "lux_sensor",
            "shadow_type": shadow_type,
            "shadow_percent": round(shadow_percent, 1),
            "confidence": confidence,
            "interpretation": interpretation,
            "root_cause": root_cause,
            "possible_causes": possible_causes,
            "lux_analysis": {
                "sensor_lux": round(sensor_lux, 1),
                "clear_sky_lux": round(clear_sky_lux, 1),
                "lux_drop_percent": round(lux_drop_fraction * 100, 1),
                "production_drop_percent": round(production_drop_fraction * 100, 1),
                "correlation_diff_percent": round(drop_difference * 100, 1),
                "threshold_used_percent": round(correlation_threshold * 100, 1),
                "sun_elevation_deg": round(sun_elevation, 1),
                "cloud_cover_percent": round(cloud_cover, 1),
            },
        }

    async def _detect_with_cloud_cover(
        self,
        prediction: Dict[str, Any],
        cloud_cover: float,
        actual_kwh: float,
        theoretical_max: float,
    ) -> Dict[str, Any]:
        """Fallback detection WITHOUT Lux sensor (cloud-based only). @zara

        Estimates expected production based on cloud cover and compares with actual.
        Less accurate than correlation-based detection but still useful.
        """
        cloud_transmission_factor = math.exp(-0.035 * cloud_cover)
        cloud_transmission_factor = max(0.03, cloud_transmission_factor)
        cloud_expected_kwh = theoretical_max * cloud_transmission_factor
        if cloud_expected_kwh > 0.01:
            production_difference_kwh = actual_kwh - cloud_expected_kwh
            deviation_percent = production_difference_kwh / cloud_expected_kwh * 100.0
        else:
            deviation_percent = 0.0
        if abs(deviation_percent) < 15:
            shadow_type = "match_forecast"
            confidence = 0.75
            interpretation = "Production matches cloud cover forecast"
            root_cause = "weather_clouds"
            possible_causes = []
        elif deviation_percent < -25:
            shadow_type = "worse_than_forecast"
            confidence = 0.7
            interpretation = f"Production lower than cloud forecast suggests ({abs(deviation_percent):.0f}% worse)"
            root_cause = "possible_obstruction"
            possible_causes = [
                "Local obstruction",
                "Panel soiling",
                "Underperforming system",
                "Thicker clouds than forecast",
            ]
        elif deviation_percent > 25:
            shadow_type = "better_than_forecast"
            confidence = 0.7
            interpretation = "Production better than cloud forecast suggests"
            root_cause = "clearer_than_forecast"
            possible_causes = []
        else:
            shadow_type = "normal"
            confidence = 0.75
            interpretation = "Normal variation from cloud-adjusted forecast"
            root_cause = "normal_variation"
            possible_causes = []
        efficiency_ratio = actual_kwh / theoretical_max if theoretical_max > 0 else 0
        shadow_percent = max(0.0, (1.0 - efficiency_ratio) * 100.0)
        return {
            "method": "sensor_fusion",
            "mode": "cloud_based",
            "shadow_type": shadow_type,
            "shadow_percent": round(shadow_percent, 1),
            "confidence": confidence,
            "interpretation": interpretation,
            "root_cause": root_cause,
            "possible_causes": possible_causes,
            "cloud_analysis": {
                "cloud_cover_percent": cloud_cover,
                "expected_kwh": round(cloud_expected_kwh, 3),
                "actual_kwh": actual_kwh,
                "deviation_percent": round(deviation_percent, 1),
            },
        }

    async def detect_shadow_ensemble(
        self, prediction: Dict[str, Any]
    ) -> Dict[str, Any]:
        "Ensemble Detection - Combines both methods for best results. @zara\n\nWeighted combination:\n- Theory Ratio: 60% (reliable baseline)\n- Sensor Fusion: 40% (adds weather/lux context)\n\nArgs:\n    prediction: Hourly prediction dict\n\nReturns:\n    Combined shadow detection result with all method outputs\n"
        try:
            theory_result = await self.detect_shadow_theory_ratio(prediction)
            sensor_result = await self.detect_shadow_sensor_fusion(prediction)
            if theory_result.get("shadow_type") == "error":
                _LOGGER.warning("Theory ratio failed, using sensor fusion only")
                return {
                    **sensor_result,
                    "ensemble_mode": "fusion_only",
                    "methods": {
                        "theory_ratio": theory_result,
                        "sensor_fusion": sensor_result,
                    },
                }
            if sensor_result.get("shadow_type") == "error":
                _LOGGER.warning("Sensor fusion failed, using theory ratio only")
                return {
                    **theory_result,
                    "ensemble_mode": "theory_only",
                    "methods": {
                        "theory_ratio": theory_result,
                        "sensor_fusion": sensor_result,
                    },
                }
            if theory_result.get("shadow_type") == "night":
                return {
                    **theory_result,
                    "ensemble_mode": "night",
                    "root_cause": "night",
                    "methods": {
                        "theory_ratio": theory_result,
                        "sensor_fusion": sensor_result,
                    },
                }
            theory_shadow_percent = theory_result.get("shadow_percent", 0)
            sensor_shadow_percent = sensor_result.get("shadow_percent", 0)
            ensemble_shadow_percent = (
                theory_shadow_percent * self.WEIGHT_THEORY
                + sensor_shadow_percent * self.WEIGHT_SENSOR
            )
            theory_confidence = theory_result.get("confidence", 0)
            sensor_confidence = sensor_result.get("confidence", 0)
            ensemble_confidence = (
                theory_confidence * self.WEIGHT_THEORY
                + sensor_confidence * self.WEIGHT_SENSOR
            )
            if ensemble_shadow_percent < self.SHADOW_THRESHOLD_LIGHT * 100:
                ensemble_shadow_type = "none"
            elif ensemble_shadow_percent < self.SHADOW_THRESHOLD_MODERATE * 100:
                ensemble_shadow_type = "light"
            elif ensemble_shadow_percent < self.SHADOW_THRESHOLD_HEAVY * 100:
                ensemble_shadow_type = "moderate"
            else:
                ensemble_shadow_type = "heavy"
            root_cause = sensor_result.get("root_cause")
            if not root_cause or root_cause == "unknown":
                possible_causes = theory_result.get("possible_causes", [])
                if possible_causes and ensemble_shadow_type in ("moderate", "heavy"):
                    root_cause = "possible_obstruction"
                elif ensemble_shadow_type == "none":
                    root_cause = "normal_variation"
                else:
                    root_cause = "unknown"
            fusion_mode = sensor_result.get("mode", "unknown")
            result = {
                "method": "ensemble",
                "ensemble_mode": "full",
                "shadow_type": ensemble_shadow_type,
                "shadow_percent": round(ensemble_shadow_percent, 1),
                "confidence": round(ensemble_confidence, 2),
                "root_cause": root_cause,
                "fusion_mode": fusion_mode,
                "efficiency_ratio": theory_result.get("efficiency_ratio", 0),
                "loss_kwh": theory_result.get("loss_kwh", 0),
                "theoretical_max_kwh": theory_result.get("theoretical_max_kwh", 0),
                "interpretation": self._generate_ensemble_interpretation(
                    ensemble_shadow_type,
                    ensemble_shadow_percent,
                    root_cause,
                    sensor_result,
                ),
                "methods": {
                    "theory_ratio": theory_result,
                    "sensor_fusion": sensor_result,
                },
                "weights": {
                    "theory": self.WEIGHT_THEORY,
                    "sensor_fusion": self.WEIGHT_SENSOR,
                },
            }
            weather_actual = prediction.get("weather_actual", {})
            if weather_actual.get("snow_covered_panels"):
                result["root_cause"] = "snow_frost"
                result["snow_detected"] = True
                result["interpretation"] = (
                    f"Snow on panels detected - {ensemble_shadow_percent:.1f}% production loss"
                )
                _LOGGER.debug(
                    "Shadow detection: Snow flag detected, overriding root_cause"
                )
            elif weather_actual.get("frost_detected") and ensemble_shadow_type in (
                "moderate",
                "heavy",
            ):
                result["root_cause"] = "panel_frost"
                result["frost_detected"] = True
                result["interpretation"] = (
                    f"Frost on panels - {ensemble_shadow_percent:.1f}% production loss"
                )
                _LOGGER.debug(
                    "Shadow detection: Frost flag detected, overriding root_cause"
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
            base_interpretation = (
                f"Excellent production ({100 - shadow_percent:.0f}% of theoretical max)"
            )
        elif shadow_type == "light":
            base_interpretation = f"Light shadowing ({shadow_percent:.0f}% loss)"
        elif shadow_type == "moderate":
            base_interpretation = f"Moderate shadowing ({shadow_percent:.0f}% loss)"
        else:
            base_interpretation = f"Heavy shadowing ({shadow_percent:.0f}% loss)"
        if root_cause == "weather_clouds":
            cause_interpretation = "primarily due to cloud cover"
        elif root_cause == "building_tree_obstruction":
            cause_interpretation = "likely due to local obstruction (building/tree)"
        elif root_cause == "normal_variation":
            cause_interpretation = "within normal operational range"
        else:
            cause_interpretation = "cause unclear, monitor system"
        return f"{base_interpretation} - {cause_interpretation}"

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
            astronomy = prediction.get("astronomy", {})
            target_hour = prediction.get("target_hour")
            if target_hour is None:
                target_hour = astronomy.get("hour")
            if target_hour is None:
                target_datetime = prediction.get("target_datetime")
                if target_datetime:
                    try:
                        if isinstance(target_datetime, str):
                            target_hour = int(
                                target_datetime.split("T")[1].split(":")[0]
                            )
                        elif hasattr(target_datetime, "hour"):
                            target_hour = target_datetime.hour
                    except (IndexError, ValueError, AttributeError):
                        pass
            if target_hour is None:
                return result
            current_month = datetime.now().month
            learned_pattern = self._pattern_learner.get_expected_shadow(
                hour=target_hour, month=current_month
            )
            if not learned_pattern.get("has_pattern"):
                result["learned_pattern"] = {
                    "available": False,
                    "message": "Pattern not yet learned for this hour",
                }
                return result
            result["learned_pattern"] = {
                "available": True,
                "pattern_type": learned_pattern.get("pattern_type"),
                "expected_shadow": learned_pattern.get("expected_shadow"),
                "expected_shadow_percent": learned_pattern.get(
                    "expected_shadow_percent"
                ),
                "is_fixed_obstruction": learned_pattern.get("is_fixed_obstruction"),
                "pattern_confidence": learned_pattern.get("confidence"),
                "dominant_cause": learned_pattern.get("dominant_cause"),
            }
            detected_shadow_percent = result.get("shadow_percent", 0)
            expected_shadow_percent = learned_pattern.get("expected_shadow_percent", 0)
            detected_shadow_type = result.get("shadow_type", "unknown")
            pattern_difference_percent = abs(
                detected_shadow_percent - expected_shadow_percent
            )
            matches_pattern = pattern_difference_percent < 15
            if matches_pattern and learned_pattern.get("confidence", 0) > 0.5:
                confidence_boost = min(0.1, learned_pattern.get("confidence", 0) * 0.15)
                previous_confidence = result.get("confidence", 0)
                result["confidence"] = min(1.0, previous_confidence + confidence_boost)
                result["learned_pattern"]["confidence_boosted"] = True
                result["learned_pattern"]["confidence_boost"] = round(
                    confidence_boost, 3
                )
            detected_cause = result.get("root_cause", "")
            protected_snow_causes = ["snow_frost", "panel_frost", "panel_frost_snow"]
            if learned_pattern.get("is_fixed_obstruction") and detected_shadow_type in (
                "moderate",
                "heavy",
            ):
                if detected_cause not in protected_snow_causes:
                    result["root_cause"] = "building_tree_obstruction"
                    result["learned_pattern"]["root_cause_from_pattern"] = True
                    result["interpretation"] = (
                        f"{result.get('interpretation', '')} (known fixed obstruction at this hour)".strip()
                    )
                else:
                    result["learned_pattern"]["pattern_overridden_by"] = detected_cause
            if not learned_pattern.get("expected_shadow"):
                if (
                    detected_shadow_type in ("moderate", "heavy")
                    and learned_pattern.get("confidence", 0) > 0.6
                ):
                    result["learned_pattern"]["unusual_shadow"] = True
                    result["learned_pattern"][
                        "unusual_message"
                    ] = f"Shadow detected but not expected based on {learned_pattern.get('confidence', 0):.0%} confident pattern"
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
        results = {}
        prediction_id = prediction.get("prediction_id", "")
        sensor_shadow_percent = 0.0
        sensor_confidence = 0.0
        if system_ensemble:
            sensor_result = system_ensemble.get("methods", {}).get("sensor_fusion", {})
            sensor_shadow_percent = sensor_result.get("shadow_percent", 0.0)
            sensor_confidence = sensor_result.get("confidence", 0.0)
        try:
            for group_name, group_actuals in panel_group_actuals.items():
                actual_kwh = group_actuals.get("actual_kwh", 0)
                theoretical_max_kwh = group_actuals.get("theoretical_max_kwh", 0)
                if theoretical_max_kwh < 0.01:
                    results[group_name] = {
                        "shadow_type": "night",
                        "shadow_percent": 0.0,
                        "confidence": 1.0,
                        "root_cause": "night",
                        "efficiency_ratio": 0.0,
                        "loss_kwh": 0.0,
                        "theoretical_max_kwh": theoretical_max_kwh,
                        "actual_kwh": actual_kwh,
                    }
                    continue
                efficiency_ratio = actual_kwh / theoretical_max_kwh
                theory_shadow_percent = max(0.0, (1.0 - efficiency_ratio) * 100.0)
                ensemble_shadow_percent = (
                    theory_shadow_percent * self.WEIGHT_THEORY
                    + sensor_shadow_percent * self.WEIGHT_SENSOR
                )
                if ensemble_shadow_percent < self.SHADOW_THRESHOLD_LIGHT * 100:
                    shadow_type = "none"
                elif ensemble_shadow_percent < self.SHADOW_THRESHOLD_MODERATE * 100:
                    shadow_type = "light"
                elif ensemble_shadow_percent < self.SHADOW_THRESHOLD_HEAVY * 100:
                    shadow_type = "moderate"
                else:
                    shadow_type = "heavy"
                root_cause = "unknown"
                if system_ensemble:
                    root_cause = system_ensemble.get("root_cause", "unknown")
                if shadow_type == "none":
                    root_cause = "normal_variation"
                theory_confidence = 0.85 if efficiency_ratio > 0 else 0.5
                confidence = (
                    theory_confidence * self.WEIGHT_THEORY
                    + sensor_confidence * self.WEIGHT_SENSOR
                )
                loss_kwh = max(0.0, theoretical_max_kwh - actual_kwh)
                group_result = {
                    "shadow_type": shadow_type,
                    "shadow_percent": round(ensemble_shadow_percent, 1),
                    "confidence": round(confidence, 2),
                    "root_cause": root_cause,
                    "efficiency_ratio": round(efficiency_ratio, 3),
                    "loss_kwh": round(loss_kwh, 3),
                    "theoretical_max_kwh": round(theoretical_max_kwh, 4),
                    "actual_kwh": round(actual_kwh, 4),
                }
                results[group_name] = group_result
                continue
            if prediction_id and results:
                await self._db.save_shadow_detection_groups(prediction_id, results)
        except Exception as error:
            _LOGGER.error("Per-group shadow detection failed: %s", error, exc_info=True)
        return results if True else None


class PerformanceLossAnalyzer(DataManagerIO):
    """Analyzes performance losses due to shading over time. @zara

    Provides daily summaries, trends, and pattern detection.
    Uses database for persistence.
    """

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        """Initialize performance loss analyzer. @zara"""
        super().__init__(hass, db_manager)
        _LOGGER.info("PerformanceLossAnalyzer initialized with DatabaseManager")

    async def analyze_daily_shadow(self, date_str: str) -> Dict[str, Any]:
        "Analyze shadow performance for a specific day from database. @zara\n\nArgs:\n    date_str: Date string (YYYY-MM-DD)\n\nReturns:\n    Daily shadow analysis summary\n"
        try:
            prediction_rows = await self.fetch_all(
                "SELECT hp.target_hour, hp.prediction_kwh, hp.actual_kwh,\n                          pa.theoretical_max_kwh\n                   FROM hourly_predictions hp\n                   LEFT JOIN prediction_astronomy pa\n                       ON hp.prediction_id = pa.prediction_id\n                   WHERE hp.target_date = ?\n                     AND hp.actual_kwh IS NOT NULL\n                     AND pa.theoretical_max_kwh > 0.01\n                   ORDER BY hp.target_hour",
                (date_str,),
            )
            if not prediction_rows:
                return {
                    "date": date_str,
                    "error": "No prediction data with actuals available",
                    "total_hours_analyzed": 0,
                }
            hourly_analysis = []
            total_loss_kwh = 0.0
            total_theoretical_kwh = 0.0
            worst_hour = None
            worst_shadow_percent = 0.0
            for prediction_row in prediction_rows:
                target_hour = prediction_row[0]
                actual_kwh = prediction_row[2] or 0
                theoretical_max_kwh = prediction_row[3] or 0
                if theoretical_max_kwh <= 0:
                    continue
                efficiency_ratio = actual_kwh / theoretical_max_kwh
                shadow_percent = max(0.0, (1.0 - efficiency_ratio) * 100.0)
                loss_kwh = theoretical_max_kwh - actual_kwh
                total_loss_kwh += loss_kwh
                total_theoretical_kwh += theoretical_max_kwh
                if shadow_percent > worst_shadow_percent:
                    worst_shadow_percent = shadow_percent
                    worst_hour = target_hour
                if shadow_percent > 15:
                    hourly_analysis.append(
                        {
                            "hour": target_hour,
                            "shadow_percent": round(shadow_percent, 1),
                            "loss_kwh": round(loss_kwh, 4),
                        }
                    )
            daily_loss_percent = (
                total_loss_kwh / total_theoretical_kwh * 100.0
                if total_theoretical_kwh > 0
                else 0.0
            )
            return {
                "date": date_str,
                "total_hours_analyzed": len(prediction_rows),
                "shadow_hours_count": len(hourly_analysis),
                "shadow_hours": [
                    hourly_result["hour"] for hourly_result in hourly_analysis
                ],
                "peak_shadow_hour": worst_hour,
                "peak_shadow_percent": round(worst_shadow_percent, 1),
                "cumulative_loss_kwh": round(total_loss_kwh, 3),
                "cumulative_theoretical_kwh": round(total_theoretical_kwh, 3),
                "daily_loss_percent": round(daily_loss_percent, 1),
                "interpretation": self._interpret_daily_shadow(
                    len(hourly_analysis), daily_loss_percent
                ),
            }
            pass
        except Exception as error:
            _LOGGER.error("Daily shadow analysis failed: %s", error, exc_info=True)
            return {"date": date_str, "error": str(error), "total_hours_analyzed": 0}

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
