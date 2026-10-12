# Powered by Solar Forecast ML by Zara-Toorox; subject to the repository LICENSE.
# Entire module verified against recovered bytecode.
# Folded return edges, where retained, preserve verified shared compiler blocks.
"""Pure shadow observation contracts shared by detection, learning, and repair."""
from __future__ import annotations
import math
from collections import defaultdict
from typing import Any, Iterable, Mapping, Optional, Sequence

CONFIRMED_SENSOR_FUSION_MODES = frozenset({"solar_radiation_sensor", "lux_sensor"})
SENSORLESS_FUSION_MODES = frozenset({"cloud_based"})
THEORY_ONLY_ENSEMBLE_MODES = frozenset({"theory_only"})
OBSERVATION_CLEAR_NEGATIVE = "clear_negative"
OBSERVATION_SENSORLESS_CANDIDATE = "sensorless_candidate"
OBSERVATION_PEER_CONFIRMED = "peer_confirmed"
OBSERVATION_CONFIRMED_SENSOR = "confirmed_sensor"
OBSERVATION_NOT_EVALUABLE = "not_evaluable"
LOCAL_OBSERVATION_CLASSES = frozenset({OBSERVATION_PEER_CONFIRMED, OBSERVATION_CONFIRMED_SENSOR})
SENSORLESS_CLEAR_CLOUD_MAX = 15.0
SENSORLESS_MIN_ELEVATION_DEG = 20.0
SENSORLESS_NEGATIVE_CONFIDENCE = 0.55
CEILING_WINDOW_DAYS = 365
CEILING_MIN_BIN_SAMPLES = 12
CEILING_MIN_AZIMUTH_SPAN_DEG = 60.0
CEILING_QUANTILE = 0.75
ELEVATION_BIN_WIDTH_DEG = 10.0
DEAD_SENSOR_RADIATION_MAX_WM2 = 5.0
DEAD_SENSOR_CLEAR_SKY_MIN_WM2 = 200.0
DEAD_SENSOR_PRODUCTION_RATIO_MIN = 0.25
MAX_SENSORLESS_PHYSICS_SHADOW_LOSS_PERCENT = 25.0
SENSORLESS_MIN_PATTERN_SAMPLES = 14


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    return number if math.isfinite(number) else None


def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered_values = sorted(values)
    middle = len(ordered_values) // 2
    if len(ordered_values) % 2:
        return float(ordered_values[middle])
    return (ordered_values[middle - 1] + ordered_values[middle]) / 2.0


def elevation_bin(elevation_deg: Optional[float]) -> Optional[int]:
    elevation = _finite(elevation_deg)
    if elevation is None or elevation < SENSORLESS_MIN_ELEVATION_DEG:
        return None
    return int(elevation // ELEVATION_BIN_WIDTH_DEG)


def azimuth_span_deg(azimuths: Iterable[float]) -> float:
    normalized_azimuths = [azimuth % 360.0 for azimuth in azimuths if math.isfinite(azimuth)]
    if len(normalized_azimuths) < 2:
        return 0.0
    ordered_values = sorted(normalized_azimuths)
    gaps = [
        ordered_values[index + 1] - ordered_values[index]
        for index in range(len(ordered_values) - 1)
    ]
    gaps.append(ordered_values[0] + 360.0 - ordered_values[-1])
    return 360.0 - max(gaps)


def weather_is_clear_enough(
    cloud_cover: Optional[float],
    precipitation_mm: Optional[float],
    sun_elevation_deg: Optional[float],
) -> bool:
    cloud = _finite(cloud_cover)
    elevation = _finite(sun_elevation_deg)
    precipitation = _finite(precipitation_mm) or 0.0
    if cloud is None or not 0.0 <= cloud <= SENSORLESS_CLEAR_CLOUD_MAX:
        return False if True else None
    if precipitation > 0.0:
        return False
    if elevation is None or elevation < SENSORLESS_MIN_ELEVATION_DEG:
        return False
    return True


def pyranometer_is_implausible(
    sensor_radiation_wm2: Optional[float],
    clear_sky_radiation_wm2: Optional[float],
    actual_kwh: Optional[float],
    theoretical_max_kwh: Optional[float],
) -> bool:
    measured_radiation = _finite(sensor_radiation_wm2)
    clear_sky_radiation = _finite(clear_sky_radiation_wm2)
    actual = _finite(actual_kwh)
    theoretical = _finite(theoretical_max_kwh)
    if (
        measured_radiation is None
        or clear_sky_radiation is None
        or actual is None
        or (theoretical is None)
    ):
        return False
    if theoretical < 0.01:
        return False
    return (
        measured_radiation <= DEAD_SENSOR_RADIATION_MAX_WM2
        and clear_sky_radiation >= DEAD_SENSOR_CLEAR_SKY_MIN_WM2
        and (actual >= theoretical * DEAD_SENSOR_PRODUCTION_RATIO_MIN)
    )


def quantile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered_values = sorted(values)
    if len(ordered_values) == 1:
        return float(ordered_values[0])
    position = max(0.0, min(1.0, fraction)) * (len(ordered_values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(ordered_values[lower])
    weight = position - lower
    return float(ordered_values[lower] * (1.0 - weight) + ordered_values[upper] * weight)


def ceiling_for_bin(ratios: Sequence[float], azimuths: Sequence[float]) -> dict[str, Any]:
    sample_count = len(ratios)
    span = azimuth_span_deg(azimuths)
    ready = sample_count >= CEILING_MIN_BIN_SAMPLES and span >= CEILING_MIN_AZIMUTH_SPAN_DEG
    return (
        (
            (
                {
                    "ready": ready,
                    "sample_count": sample_count,
                    "azimuth_span_deg": round(span, 1),
                    "ceiling_ratio": (
                        round(quantile(ratios, CEILING_QUANTILE), 4) if ratios else None
                    ),
                    "reason": (
                        "ready"
                        if ready
                        else (
                            "insufficient_azimuth_diversity"
                            if sample_count >= CEILING_MIN_BIN_SAMPLES
                            else "insufficient_bin_samples"
                        )
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


def classify_observation_class(
    *,
    fusion_mode: Optional[str],
    ensemble_mode: Optional[str],
    root_cause: Optional[str],
    group_name: str = "_system_",
    evaluable: bool = False,
    shadow_candidate: bool = False,
) -> str:
    fusion = str(fusion_mode or "")
    ensemble = str(ensemble_mode or "")
    cause = str(root_cause or "")
    if ensemble in THEORY_ONLY_ENSEMBLE_MODES:
        return OBSERVATION_NOT_EVALUABLE
    if not evaluable:
        return OBSERVATION_NOT_EVALUABLE
    if cause == "building_tree_obstruction" and fusion in CONFIRMED_SENSOR_FUSION_MODES:
        return OBSERVATION_CONFIRMED_SENSOR
    if cause == "possible_obstruction" and fusion in CONFIRMED_SENSOR_FUSION_MODES:
        return OBSERVATION_CONFIRMED_SENSOR
    if cause == "possible_obstruction":
        if group_name != "_system_" or fusion == "peer_group":
            return OBSERVATION_PEER_CONFIRMED
    if fusion in SENSORLESS_FUSION_MODES:
        if shadow_candidate:
            return OBSERVATION_SENSORLESS_CANDIDATE
        return OBSERVATION_CLEAR_NEGATIVE
    if shadow_candidate and cause in {"possible_obstruction", "building_tree_obstruction"}:
        return (
            OBSERVATION_CONFIRMED_SENSOR
            if fusion in CONFIRMED_SENSOR_FUSION_MODES
            else OBSERVATION_NOT_EVALUABLE
        )
    return OBSERVATION_CLEAR_NEGATIVE


def shadow_detected_from_observation(observation_class: str) -> bool:
    return observation_class in LOCAL_OBSERVATION_CLASSES


def is_local_learning_cause(
    *,
    root_cause: Optional[str],
    observation_class: Optional[str],
    fusion_mode: Optional[str] = None,
    group_name: str = "_system_",
    shadow_detected: bool = False,
) -> bool:
    observation = str(observation_class or "")
    if observation in LOCAL_OBSERVATION_CLASSES:
        return True
    cause = str(root_cause or "")
    if cause == "building_tree_obstruction":
        return True
    if cause == "possible_obstruction":
        if observation == OBSERVATION_SENSORLESS_CANDIDATE:
            return False
        if str(fusion_mode or "") in SENSORLESS_FUSION_MODES:
            return False
        if group_name != "_system_":
            return True
        if str(fusion_mode or "") in CONFIRMED_SENSOR_FUSION_MODES:
            return True
    return False


def classify_sensorless_hour(
    *,
    cloud_cover: Optional[float],
    precipitation_mm: Optional[float],
    sun_elevation_deg: Optional[float],
    actual_kwh: Optional[float],
    theoretical_max_kwh: Optional[float],
    ceiling_ready: bool = False,
    ceiling_ratio: Optional[float] = None,
    residual_threshold: float = 0.3,
) -> dict[str, Any]:
    clear_sky_loss = 0.0
    actual = _finite(actual_kwh)
    theoretical = _finite(theoretical_max_kwh)
    if actual is not None and theoretical is not None and (theoretical > 0.01):
        clear_sky_loss = max(0.0, min(100.0, (1.0 - actual / theoretical) * 100.0))
    if not weather_is_clear_enough(cloud_cover, precipitation_mm, sun_elevation_deg):
        cloud = _finite(cloud_cover)
        return (
            {
                "mode": "cloud_based",
                "shadow_type": "not_evaluable",
                "shadow_percent": 0.0,
                "confidence": 0.0,
                "root_cause": "weather_clouds" if cloud is not None else "insufficient_reference",
                "evaluable": False,
                "learning_eligible": False,
                "observation_class": OBSERVATION_NOT_EVALUABLE,
                "local_shadow_percent": 0.0,
                "clear_sky_loss_percent": round(clear_sky_loss, 1),
                "weather_loss_percent": round(cloud, 1) if cloud is not None else None,
                "interpretation": "Weather loss is visible, but local shadow cannot be evaluated",
            }
            if True
            else None
        )
    residual = None
    if (
        ceiling_ready
        and ceiling_ratio is not None
        and (ceiling_ratio > 0.0)
        and (actual is not None)
        and (theoretical is not None)
        and (theoretical > 0.01)
    ):
        residual = max(0.0, 1.0 - actual / theoretical / ceiling_ratio)
    candidate = bool(ceiling_ready and residual is not None and (residual >= residual_threshold))
    observation_class = (
        OBSERVATION_SENSORLESS_CANDIDATE if candidate else OBSERVATION_CLEAR_NEGATIVE
    )
    return (
        (
            (
                {
                    "mode": "cloud_based",
                    "shadow_type": "none",
                    "shadow_percent": 0.0,
                    "confidence": SENSORLESS_NEGATIVE_CONFIDENCE,
                    "root_cause": "normal_variation",
                    "evaluable": True,
                    "learning_eligible": True,
                    "observation_class": observation_class,
                    "local_shadow_percent": 0.0,
                    "clear_sky_loss_percent": round(clear_sky_loss, 1),
                    "weather_loss_percent": 0.0,
                    "ceiling_ready": ceiling_ready,
                    "ceiling_residual_ratio": round(residual, 4) if residual is not None else None,
                    "interpretation": (
                        "Clear-weather hour stored as a negative observation"
                        if not candidate
                        else "Sensorless residual against geometry ceiling remains unpromoted"
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


def build_geometry_ceilings(samples: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    bins = defaultdict(list)
    for sample in samples:
        elevation = _finite(sample.get("sun_elevation_deg"))
        azimuth = _finite(sample.get("sun_azimuth_deg"))
        actual = _finite(sample.get("actual_kwh"))
        theoretical = _finite(sample.get("theoretical_max_kwh"))
        bin_index = elevation_bin(elevation)
        if (
            bin_index is None
            or azimuth is None
            or actual is None
            or (theoretical is None)
            or (theoretical < 0.01)
        ):
            continue
        bins[bin_index].append((actual / theoretical, azimuth))
    ceilings = {}
    for bin_index, rows in bins.items():
        ratios = [row[0] for row in rows]
        azimuths = [row[1] for row in rows]
        ceiling = ceiling_for_bin(ratios, azimuths)
        ceiling["elevation_bin"] = bin_index
        ceiling["elevation_min_deg"] = bin_index * ELEVATION_BIN_WIDTH_DEG
        ceiling["elevation_max_deg"] = (bin_index + 1) * ELEVATION_BIN_WIDTH_DEG
        ceilings[bin_index] = ceiling
    return ceilings
