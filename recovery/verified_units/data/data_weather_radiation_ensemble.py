# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Horizon-aware solar radiation ensemble for the weather forecast pipeline."""
from __future__ import annotations
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, Sequence

GHI_MIN = 0.0
GHI_MAX = 1600.0
CLOUD_RESIDUAL_MIN_SCALE = 0.88
CLOUD_RESIDUAL_MAX_SCALE = 1.12
CLOUD_RESIDUAL_MIN_DELTA = 8.0
BRIGHT_SKY_KWH_TO_WM2 = 1000.0
RADIATION_HOUR_BASIS_INTERVAL_START = "interval_start"
PROVIDER_RADIATION_FIELDS = (
    "ghi",
    "direct_radiation",
    "diffuse_radiation",
    "global_tilted_irradiance",
    "ghi_icon_d2",
    "direct_radiation_icon_d2",
    "diffuse_radiation_icon_d2",
    "ghi_icon_eu",
    "direct_radiation_icon_eu",
    "diffuse_radiation_icon_eu",
)
MLP_RESIDUAL_MIN = 0.5
MLP_RESIDUAL_MAX = 1.5


def blend_static_with_mlp_residual(
    static_factor: float, mlp_residual: float, confidence: float
) -> float:
    """Apply the MLP residual on top of the static solar factor, faded by confidence.

    confidence 0 keeps the static factor, confidence 1 applies the full
    residual; the residual itself is bounded so a single bad prediction
    cannot swing the correction outside [0.5, 1.5] of the static factor.
    """
    bounded_residual = max(MLP_RESIDUAL_MIN, min(MLP_RESIDUAL_MAX, float(mlp_residual)))
    bounded_confidence = max(0.0, min(1.0, float(confidence)))
    return float(static_factor) * (1.0 + bounded_confidence * (bounded_residual - 1.0))


def provider_label_for_interval_start(date_str: str, hour: int) -> tuple[str, int]:
    """Return the delivered (date, hour) label whose preceding-hour mean covers [hour, hour+1)."""
    hour_number = int(hour)
    if hour_number < 23:
        return (str(date_str), hour_number + 1)
    next_date = datetime.strptime(str(date_str), "%Y-%m-%d") + timedelta(days=1)
    return (next_date.strftime("%Y-%m-%d"), 0)


def align_provider_radiation_to_interval_start(
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Move provider radiation fields one hour earlier so entry h covers [h, h+1).

    Non-radiation fields stay on their delivered hour. The last hour of the
    horizon has no successor and gets ``None`` radiation. Entries already
    marked as interval-start are returned unchanged, so the transform is safe
    to apply once per reader.
    """
    entries_by_hour = {}
    for entry in entries or []:
        if entry.get("date") is None or entry.get("hour") is None:
            continue
        entries_by_hour[str(entry["date"]), int(entry["hour"])] = entry
    aligned_entries = []
    for entry in entries or []:
        aligned_entry = dict(entry)
        if (
            aligned_entry.get("date") is None
            or aligned_entry.get("hour") is None
            or aligned_entry.get("radiation_hour_basis")
            == RADIATION_HOUR_BASIS_INTERVAL_START
        ):
            aligned_entries.append(aligned_entry)
        else:
            successor = entries_by_hour.get(
                provider_label_for_interval_start(
                    str(aligned_entry["date"]), int(aligned_entry["hour"])
                )
            )
            for radiation_field in PROVIDER_RADIATION_FIELDS:
                if radiation_field not in aligned_entry:
                    if successor is None or radiation_field not in successor:
                        continue
                aligned_entry[radiation_field] = (
                    successor.get(radiation_field) if successor is not None else None
                )
            aligned_entry["radiation_hour_basis"] = RADIATION_HOUR_BASIS_INTERVAL_START
            aligned_entries.append(aligned_entry)
    return aligned_entries


SOURCE_IFS = "ifs"
SOURCE_ICON_D2 = "icon_d2"
SOURCE_ICON_EU = "icon_eu"
SOURCE_BRIGHT_SKY = "bright_sky"
SOURCE_IDENTITY_ECMWF_IFS = "ecmwf_ifs025"
SOURCE_IDENTITY_DWD_ICON = "dwd_icon_endpoint"
SOURCE_IDENTITY_OPEN_METEO_DEFAULT = "open_meteo_default"
SNAPSHOT_KIND_PROVIDER_FETCH = "provider_fetch"
SNAPSHOT_KIND_ENSEMBLE_COMPOSE = "ensemble_compose"
_WEIGHT_BANDS: tuple[tuple[int, dict[str, float]], ...] = (
    (
        6,
        {
            SOURCE_ICON_D2: 0.55,
            SOURCE_IFS: 0.3,
            SOURCE_BRIGHT_SKY: 0.15,
            SOURCE_ICON_EU: 0.0,
        },
    ),
    (
        24,
        {
            SOURCE_ICON_D2: 0.45,
            SOURCE_IFS: 0.35,
            SOURCE_BRIGHT_SKY: 0.1,
            SOURCE_ICON_EU: 0.1,
        },
    ),
    (
        48,
        {
            SOURCE_ICON_D2: 0.0,
            SOURCE_IFS: 0.5,
            SOURCE_BRIGHT_SKY: 0.1,
            SOURCE_ICON_EU: 0.4,
        },
    ),
    (
        1000000000,
        {
            SOURCE_ICON_D2: 0.0,
            SOURCE_IFS: 0.7,
            SOURCE_BRIGHT_SKY: 0.0,
            SOURCE_ICON_EU: 0.3,
        },
    ),
)


@dataclass(frozen=True)
class RadiationSources:
    """Per-hour raw radiation inputs. Missing models stay None."""

    ghi_ifs: Optional[float] = None
    dni_ifs: Optional[float] = None
    dhi_ifs: Optional[float] = None
    ghi_icon_d2: Optional[float] = None
    dni_icon_d2: Optional[float] = None
    dhi_icon_d2: Optional[float] = None
    ghi_icon_eu: Optional[float] = None
    dni_icon_eu: Optional[float] = None
    dhi_icon_eu: Optional[float] = None
    ghi_bright_sky: Optional[float] = None
    ifs_cloud: Optional[float] = None
    blended_cloud: Optional[float] = None
    ifs_source_identity: Optional[str] = None


@dataclass(frozen=True)
class EnsembleRadiation:
    """Corrector-ready radiation after horizon blend and cloud residual."""

    ghi: float
    dni: float
    dhi: float
    used_sources: tuple[str, ...]
    weights_used: dict[str, float]
    lead_hours: int
    cloud_residual_scale: float
    kalman_applied: bool
    icon_d2_available: bool
    raw_blend_ghi: float
    kalman_bias: float


def clamp_ghi(value: float) -> float:
    return max(GHI_MIN, min(GHI_MAX, value))


def _exact_rounded_partition(total: float, first: float) -> tuple[float, float]:
    candidates = (first, math.nextafter(first, total), math.nextafter(first, 0.0))
    for direct_component in candidates:
        diffuse_component = total - direct_component
        if 0.0 <= direct_component <= total:
            if 0.0 <= diffuse_component <= total:
                if direct_component + diffuse_component == total:
                    return (direct_component, diffuse_component)
    return (0.0, total)


def bright_sky_solar_to_wm2(solar_kwh_m2: Optional[float]) -> Optional[float]:
    """Convert Bright Sky hourly solar energy (kWh/m²) to mean W/m²."""
    if solar_kwh_m2 is None:
        return
    try:
        solar_energy = float(solar_kwh_m2)
    except (TypeError, ValueError):
        return
    if solar_energy < 0:
        return
    return clamp_ghi(solar_energy * BRIGHT_SKY_KWH_TO_WM2)


def open_meteo_base_source_identity(provider_source: Any) -> str:
    """Return the actual provider family behind the unsuffixed base radiation."""
    provider_name = str(provider_source or "").strip().lower()
    if "dwd-icon" in provider_name:
        return SOURCE_IDENTITY_DWD_ICON
    if "multi-model" in provider_name or "ecmwf" in provider_name:
        return SOURCE_IDENTITY_ECMWF_IFS
    return SOURCE_IDENTITY_OPEN_METEO_DEFAULT


def forecast_lead_hours(forecast_date: str, hour: int, now: datetime) -> int:
    """Whole hours from the current hour floor to the forecast hour."""
    target_datetime = datetime(
        int(forecast_date[0:4]),
        int(forecast_date[5:7]),
        int(forecast_date[8:10]),
        int(hour),
        0,
        0,
        tzinfo=now.tzinfo,
    )
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    return int((target_datetime - current_hour).total_seconds() // 3600)


def should_apply_kalman(
    forecast_date: str, hour: int, now: datetime, sun_elevation: float
) -> bool:
    """Kalman bias is an intraday state and must not leak into D+1/D+2."""
    if sun_elevation <= 5:
        return False
    today_iso = now.date().isoformat()
    if forecast_date != today_iso:
        return False
    return hour >= now.hour


def apply_terminal_kalman_ghi(
    p0: float, requested_bias: float, *, cap_ceiling: Optional[float] = None
) -> tuple[float, float]:
    """Apply Kalman after the solar factor and return (final_ghi, applied_bias).

    The recorded bias is the delivered delta, so
    ``final_ghi - applied_bias`` reconstructs ``p0`` at the floor and ceiling.
    A positive request cannot raise GHI above the coherence-cap ceiling.
    """
    p0 = clamp_ghi(p0)
    try:
        bias_request = float(requested_bias or 0.0)
    except (TypeError, ValueError):
        bias_request = 0.0
    if not math.isfinite(bias_request):
        bias_request = 0.0
    limited_ghi = clamp_ghi(p0 + bias_request)
    if bias_request > 0.0 and cap_ceiling is not None:
        try:
            coherence_ceiling = float(cap_ceiling)
        except (TypeError, ValueError):
            coherence_ceiling = None
        else:
            if math.isfinite(coherence_ceiling) and coherence_ceiling >= 0.0:
                limited_ghi = min(limited_ghi, max(p0, clamp_ghi(coherence_ceiling)))
    final_ghi = round(limited_ghi, 1)
    applied_bias = round(final_ghi - p0, 4)
    return (final_ghi, applied_bias)


def horizon_source_weights(lead_hours: int) -> dict[str, float]:
    for lead_limit, source_weights in _WEIGHT_BANDS:
        if not lead_hours < lead_limit:
            continue
        return dict(source_weights)
    return dict(_WEIGHT_BANDS[-1][1])


def _finite_nonneg(value: Optional[float]) -> Optional[float]:
    if value is None:
        return
    try:
        parsed_value = float(value)
    except (TypeError, ValueError):
        return
    if parsed_value != parsed_value or parsed_value < 0:
        return None
    return parsed_value


def _ghi_map(sources: RadiationSources) -> dict[str, float]:
    source_values = {
        SOURCE_IFS: _finite_nonneg(sources.ghi_ifs),
        SOURCE_ICON_D2: _finite_nonneg(sources.ghi_icon_d2),
        SOURCE_ICON_EU: _finite_nonneg(sources.ghi_icon_eu),
        SOURCE_BRIGHT_SKY: _finite_nonneg(sources.ghi_bright_sky),
    }
    return {
        source_name: source_ghi
        for source_name, source_ghi in source_values.items()
        if source_ghi is not None
    }


def _dni_dhi_map(sources: RadiationSources) -> dict[str, tuple[float, float, float]]:
    source_components = {
        SOURCE_IFS: (
            _finite_nonneg(sources.ghi_ifs),
            _finite_nonneg(sources.dni_ifs),
            _finite_nonneg(sources.dhi_ifs),
        ),
        SOURCE_ICON_D2: (
            _finite_nonneg(sources.ghi_icon_d2),
            _finite_nonneg(sources.dni_icon_d2),
            _finite_nonneg(sources.dhi_icon_d2),
        ),
        SOURCE_ICON_EU: (
            _finite_nonneg(sources.ghi_icon_eu),
            _finite_nonneg(sources.dni_icon_eu),
            _finite_nonneg(sources.dhi_icon_eu),
        ),
    }
    complete_components = {}
    for source_name, (ghi, dni, dhi) in source_components.items():
        if ghi is None or dni is None or dhi is None:
            continue
        complete_components[source_name] = (ghi, dni, dhi)
    return complete_components


def _renormalize(
    weights: Mapping[str, float], available: Mapping[str, float]
) -> dict[str, float]:
    positive_weights = {
        source_name: weight
        for source_name, weight in weights.items()
        if weight > 0
        if source_name in available
    }
    total_weight = sum(positive_weights.values())
    if total_weight <= 0:
        if not available:
            return {}
        uniform_weight = 1.0 / len(available)
        return {source_name: uniform_weight for source_name in available}
    return {
        source_name: weight / total_weight
        for source_name, weight in positive_weights.items()
    }


def _weighted_sum(
    values: Mapping[str, float], weights: Mapping[str, float]
) -> Optional[float]:
    if not weights:
        return
    return sum(
        (values[source_name] * weight for source_name, weight in weights.items())
    )


def cloud_residual_scale(
    ifs_cloud: Optional[float],
    blended_cloud: Optional[float],
    *,
    icon_d2_available: bool,
) -> float:
    """Scale IFS GHI only when ICON-D2 GHI is missing and clouds disagree."""
    if icon_d2_available:
        return 1.0
    ifs_cloud_value = _finite_nonneg(ifs_cloud)
    blended_cloud_value = _finite_nonneg(blended_cloud)
    if ifs_cloud_value is None or blended_cloud_value is None:
        return 1.0
    if abs(ifs_cloud_value - blended_cloud_value) < CLOUD_RESIDUAL_MIN_DELTA:
        return 1.0
    ifs_clear_fraction = max(0.08, 1.0 - min(100.0, ifs_cloud_value) / 100.0)
    blended_clear_fraction = max(0.05, 1.0 - min(100.0, blended_cloud_value) / 100.0)
    residual_ratio = blended_clear_fraction / ifs_clear_fraction
    return max(CLOUD_RESIDUAL_MIN_SCALE, min(CLOUD_RESIDUAL_MAX_SCALE, residual_ratio))


def _split_from_nwp(ghi: float, dni: float, dhi: float) -> tuple[float, float]:
    normalization_ghi = ghi if ghi > 1.0 else dni + dhi
    if normalization_ghi <= 1.0:
        return (0.0, 0.0)
    return (dni / normalization_ghi, dhi / normalization_ghi)


def compose_ensemble_radiation(
    sources: RadiationSources,
    *,
    forecast_date: str,
    hour: int,
    now: datetime,
    sun_elevation: float,
    kalman_bias: float = 0.0,
) -> EnsembleRadiation:
    """Blend independent GHI sources and keep NWP DNI/DHI.

    ``kalman_bias`` is ignored here. Terminal Kalman is applied after the
    solar factor and coherence cap so the delivered delta reconstructs P0.
    """
    lead_hours = forecast_lead_hours(forecast_date, hour, now)
    ghi_by_source = _ghi_map(sources)
    icon_d2_available = SOURCE_ICON_D2 in ghi_by_source
    source_weights = _renormalize(horizon_source_weights(lead_hours), ghi_by_source)
    blended_ghi = _weighted_sum(ghi_by_source, source_weights)
    if blended_ghi is None:
        blended_ghi = 0.0
    raw_blend_ghi = blended_ghi
    cloud_scale = cloud_residual_scale(
        sources.ifs_cloud, sources.blended_cloud, icon_d2_available=icon_d2_available
    )
    blended_ghi *= cloud_scale
    intraday_inputs = (sun_elevation, kalman_bias)
    ghi = clamp_ghi(blended_ghi)
    nwp_components = _dni_dhi_map(sources)
    nwp_weights = {
        source_name: weight
        for source_name, weight in source_weights.items()
        if source_name in nwp_components
    }
    nwp_weight_total = sum(nwp_weights.values())
    if nwp_weight_total > 0:
        nwp_weights = {
            source_name: weight / nwp_weight_total
            for source_name, weight in nwp_weights.items()
        }
        direct_fraction = 0.0
        diffuse_fraction = 0.0
        for source_name, weight in nwp_weights.items():
            model_ghi, model_dni, model_dhi = nwp_components[source_name]
            model_direct_fraction, model_diffuse_fraction = _split_from_nwp(
                model_ghi, model_dni, model_dhi
            )
            direct_fraction += model_direct_fraction * weight
            diffuse_fraction += model_diffuse_fraction * weight
    else:
        direct_fraction, diffuse_fraction = (0.0, 0.0)
    fraction_total = direct_fraction + diffuse_fraction
    if fraction_total > 0:
        direct_fraction /= fraction_total
        diffuse_fraction /= fraction_total
    elif ghi > 0:
        diffuse_fraction = 1.0
    rounded_ghi = round(ghi, 1)
    rounded_dni = min(rounded_ghi, round(rounded_ghi * direct_fraction, 1))
    rounded_dni, rounded_dhi = _exact_rounded_partition(rounded_ghi, rounded_dni)
    return EnsembleRadiation(
        ghi=rounded_ghi,
        dni=rounded_dni,
        dhi=rounded_dhi,
        used_sources=tuple(sorted(source_weights)),
        weights_used={
            source_name: round(weight, 4)
            for source_name, weight in source_weights.items()
        },
        lead_hours=lead_hours,
        cloud_residual_scale=round(cloud_scale, 4),
        kalman_applied=False,
        icon_d2_available=icon_d2_available,
        raw_blend_ghi=round(clamp_ghi(raw_blend_ghi), 1),
        kalman_bias=0.0,
    )


def estimate_hourly_kwh(
    ghi_wm2: float, *, capacity_kwp: float = 10.0, performance_ratio: float = 0.8
) -> float:
    """First-order PV energy from GHI. Not a site-specific yield model."""
    return round(max(0.0, ghi_wm2) / 1000.0 * capacity_kwp * performance_ratio, 3)


def legacy_ifs_only_ghi(
    sources: RadiationSources, *, kalman_bias: float, apply_kalman: bool
) -> float:
    """Reproduce the previous corrector radiation path for gain comparison."""
    ghi = _finite_nonneg(sources.ghi_ifs) or 0.0
    if apply_kalman and kalman_bias:
        ghi = max(0.0, ghi + kalman_bias)
    return clamp_ghi(ghi)


_INDEPENDENT_GHI_SOURCES = (
    SOURCE_IFS,
    SOURCE_ICON_D2,
    SOURCE_ICON_EU,
    SOURCE_BRIGHT_SKY,
)
MIN_WEATHER_HOURS_TO_LOCK_DAY = 24
MIN_PREDICTION_HOURS_TO_LOCK_DAY = 8
MIN_LOCAL_SOLAR_CALIBRATION_DAYS = 7
MIN_LOCAL_SOLAR_CALIBRATION_CONFIDENCE = 0.75
MAX_ENSEMBLE_SOLAR_CALIBRATION_WEIGHT = 0.65
WEATHER_GHI_LOCAL_PRIOR_MIGRATION_ID = "weather_ghi_local_prior_v20260824"
GHI_UPDATE_PRIOR_WINDOW_DAYS = MIN_LOCAL_SOLAR_CALIBRATION_DAYS
GHI_UPDATE_PRIOR_MIN_HOURS = 3
GHI_UPDATE_PRIOR_MIN_FORECAST_WM2 = 20.0
GHI_UPDATE_PRIOR_HOUR_MIN = 7
GHI_UPDATE_PRIOR_HOUR_MAX = 17
GHI_UPDATE_PRIOR_FACTOR_MIN = 0.25
GHI_UPDATE_PRIOR_FACTOR_MAX = 2.0


def hourly_forecast_date_covered(
    hourly_values: list[dict[str, Any]],
    target_date: str,
    minimum_hours: int = MIN_PREDICTION_HOURS_TO_LOCK_DAY,
) -> bool:
    """Return whether an aggregate has enough distinct hourly source rows."""
    covered_hours = set()
    for entry in hourly_values or []:
        date_key = entry.get("date", entry.get("target_date"))
        if str(date_key or "") != target_date:
            continue
        raw_hour = entry.get("hour", entry.get("target_hour"))
        try:
            hour = int(raw_hour)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23:
            covered_hours.add(hour)
    return len(covered_hours) >= minimum_hours


def _independent_ghi_sources(used_sources: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        (
            source_name
            for source_name in used_sources
            if source_name in _INDEPENDENT_GHI_SOURCES
        )
    )


def compute_ghi_update_prior(
    samples: Sequence[tuple[Any, Any, Any, Any]],
) -> Optional[dict[str, Any]]:
    """Return a one-shot local GHI prior from forecast/actual pairs.

    Requires a full calibration window of local GHI actuals. Installations
    without a radiation sensor stay neutral. The confidence floor lets the
    existing apply-gate use this prior immediately after an update reset.
    """
    pairs_by_date = {}
    for date_key, raw_hour, raw_forecast, raw_actual in samples or ():
        try:
            hour = int(raw_hour)
            forecast_ghi = float(raw_forecast)
            actual_ghi = float(raw_actual)
        except (TypeError, ValueError):
            continue
        if hour < GHI_UPDATE_PRIOR_HOUR_MIN or hour > GHI_UPDATE_PRIOR_HOUR_MAX:
            continue
        if not (math.isfinite(forecast_ghi) and math.isfinite(actual_ghi)):
            continue
        if forecast_ghi <= GHI_UPDATE_PRIOR_MIN_FORECAST_WM2 or actual_ghi < 0.0:
            continue
        pairs_by_date.setdefault(str(date_key), []).append((forecast_ghi, actual_ghi))
    day_ratios = []
    total_forecast = 0.0
    total_actual = 0.0
    for day_pairs in pairs_by_date.values():
        if len(day_pairs) < GHI_UPDATE_PRIOR_MIN_HOURS:
            continue
        day_forecast_total = sum(
            (forecast_ghi for forecast_ghi, unused_actual in day_pairs)
        )
        day_actual_total = sum(
            (actual_ghi for unused_forecast, actual_ghi in day_pairs)
        )
        if day_forecast_total <= 0.0:
            continue
        day_ratios.append(day_actual_total / day_forecast_total)
        total_forecast += day_forecast_total
        total_actual += day_actual_total
    sample_day_count = len(day_ratios)
    if sample_day_count < MIN_LOCAL_SOLAR_CALIBRATION_DAYS or total_forecast <= 0.0:
        return None
    solar_factor = max(
        GHI_UPDATE_PRIOR_FACTOR_MIN,
        min(GHI_UPDATE_PRIOR_FACTOR_MAX, total_actual / total_forecast),
    )
    mean_day_ratio = sum(day_ratios) / sample_day_count
    ratio_variance = (
        sum(((day_ratio - mean_day_ratio) ** 2 for day_ratio in day_ratios))
        / sample_day_count
    )
    ratio_std_dev = math.sqrt(ratio_variance)
    sample_confidence = min(
        1.0, sample_day_count / float(MIN_LOCAL_SOLAR_CALIBRATION_DAYS)
    )
    stability_confidence = max(0.0, 1.0 - ratio_std_dev * 2.0)
    prior_confidence = max(
        MIN_LOCAL_SOLAR_CALIBRATION_CONFIDENCE,
        (sample_confidence + stability_confidence) / 2.0,
    )
    return {
        "solar_factor": round(solar_factor, 3),
        "sample_days": int(sample_day_count),
        "confidence": round(prior_confidence, 2),
    }


def solar_precision_window_is_apply_ready(sample_days: int, confidence: float) -> bool:
    """Return whether a stored GHI factor has a full apply-ready window."""
    try:
        valid_sample_days = int(sample_days or 0)
        valid_confidence = float(confidence or 0.0)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(valid_confidence)
        and valid_sample_days >= MIN_LOCAL_SOLAR_CALIBRATION_DAYS
        and (valid_confidence >= MIN_LOCAL_SOLAR_CALIBRATION_CONFIDENCE)
    )


def resolve_preserved_solar_precision_state(
    *,
    rolling_solar_factor: float,
    rolling_sample_days: int,
    rolling_confidence: float,
    stored_solar_factor: Optional[float],
    stored_sample_days: Optional[int],
    stored_confidence: Optional[float],
) -> tuple[float, int, float, bool]:
    """Keep a stored GHI factor until rolling evidence itself is apply-ready."""
    if solar_precision_window_is_apply_ready(rolling_sample_days, rolling_confidence):
        return (
            float(rolling_solar_factor),
            int(rolling_sample_days),
            float(rolling_confidence),
            False,
        )
    if stored_solar_factor is None:
        return (
            float(rolling_solar_factor),
            int(rolling_sample_days),
            float(rolling_confidence),
            False,
        )
    return (
        float(stored_solar_factor),
        int(stored_sample_days or 0),
        float(stored_confidence or 0.0),
        True,
    )


def should_apply_solar_precision_factor(
    used_sources: tuple[str, ...],
    *,
    ghi_from_ensemble: bool,
    local_observation_available: bool = False,
    sample_days: int = 0,
    confidence: float = 0.0,
) -> bool:
    """Return whether a learned local GHI factor is safe for this forecast.

    Every path requires a currently available physical radiation sensor.
    Installations without that sensor stay neutral. Legacy and IFS-only
    radiation may then use the stored factor; other ensemble compositions
    also need a full, high-confidence learning window.
    """
    if not local_observation_available:
        return False
    if not ghi_from_ensemble:
        return True
    if set(_independent_ghi_sources(used_sources)) <= {SOURCE_IFS}:
        return True
    return solar_precision_window_is_apply_ready(sample_days, confidence)


def shrink_ensemble_solar_precision_factor(
    factor: float, *, sample_days: int, confidence: float
) -> float:
    """Shrink local ensemble calibration toward neutral for portability."""
    try:
        valid_factor = float(factor)
        valid_sample_days = max(0, int(sample_days or 0))
        valid_confidence = float(confidence or 0.0)
    except (TypeError, ValueError):
        return 1.0
    if not (
        math.isfinite(valid_factor)
        and math.isfinite(valid_confidence)
        and should_apply_solar_precision_factor(
            (SOURCE_ICON_D2, SOURCE_IFS),
            ghi_from_ensemble=True,
            local_observation_available=True,
            sample_days=valid_sample_days,
            confidence=valid_confidence,
        )
    ):
        return 1.0
    valid_confidence = max(0.0, min(1.0, valid_confidence))
    window_weight = min(1.0, valid_sample_days / MIN_LOCAL_SOLAR_CALIBRATION_DAYS)
    calibration_weight = min(
        MAX_ENSEMBLE_SOLAR_CALIBRATION_WEIGHT,
        MAX_ENSEMBLE_SOLAR_CALIBRATION_WEIGHT * valid_confidence * window_weight,
    )
    return 1.0 + (valid_factor - 1.0) * calibration_weight


def should_apply_coherence_cap(
    used_sources: tuple[str, ...], *, ghi_from_ensemble: bool
) -> bool:
    """Dense-overcast cap must not override a multi-model GHI mix."""
    if not ghi_from_ensemble:
        return True
    return len(_independent_ghi_sources(used_sources)) < 2


def strategy_ghi_coherence_cap_allowed(eligible) -> bool:
    """Strategy cap runs on legacy NULL rows and when eligible=1 (cap allowed).

    eligible=0 means the corrector already decided the mix has enough
    independent GHI sources. Unknown values fail closed to the old cap.
    """
    if eligible is None:
        return True
    try:
        return int(eligible) == 1
    except (TypeError, ValueError):
        return True


def morning_day_lock_allowed(weather_hours: int, prediction_hours: int) -> bool:
    """Lock a morning day only when weather and predictions cover that date."""
    try:
        weather_hour_count = int(weather_hours or 0)
        prediction_hour_count = int(prediction_hours or 0)
    except (TypeError, ValueError):
        return False
    return (
        weather_hour_count >= MIN_WEATHER_HOURS_TO_LOCK_DAY
        and prediction_hour_count >= MIN_PREDICTION_HOURS_TO_LOCK_DAY
    )


def snapshot_target_datetime(forecast_date: str, hour: int, now: datetime) -> str:
    """ISO timestamp for one forecast hour in the retrieve timezone."""
    target_datetime = datetime(
        int(forecast_date[0:4]),
        int(forecast_date[5:7]),
        int(forecast_date[8:10]),
        int(hour),
        0,
        0,
        tzinfo=now.tzinfo,
    )
    return target_datetime.isoformat()


def _source_row(
    source_name: str,
    ghi: Optional[float],
    dni: Optional[float],
    dhi: Optional[float],
    weight_used: Optional[float] = None,
    source_identity: Optional[str] = None,
) -> dict[str, Any]:
    available = ghi is not None
    return (
        (
            (
                {
                    "source_name": source_name,
                    "source_identity": source_identity or source_name,
                    "available": available,
                    "ghi": ghi,
                    "dni": dni if available else None,
                    "dhi": dhi if available else None,
                    "weight_used": weight_used if available else None,
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


def provider_fetch_source_rows_open_meteo(
    entry: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Raw Open-Meteo radiation members before the cache upsert."""
    return [
        _source_row(
            SOURCE_IFS,
            _finite_nonneg(entry.get("ghi")),
            _finite_nonneg(entry.get("direct_radiation")),
            _finite_nonneg(entry.get("diffuse_radiation")),
            source_identity=str(entry.get("ghi_source_identity") or "").strip()
            or open_meteo_base_source_identity(entry.get("source")),
        ),
        _source_row(
            SOURCE_ICON_D2,
            _finite_nonneg(entry.get("ghi_icon_d2")),
            _finite_nonneg(entry.get("direct_radiation_icon_d2")),
            _finite_nonneg(entry.get("diffuse_radiation_icon_d2")),
            source_identity=SOURCE_ICON_D2,
        ),
        _source_row(
            SOURCE_ICON_EU,
            _finite_nonneg(entry.get("ghi_icon_eu")),
            _finite_nonneg(entry.get("direct_radiation_icon_eu")),
            _finite_nonneg(entry.get("diffuse_radiation_icon_eu")),
            source_identity=SOURCE_ICON_EU,
        ),
    ]


def provider_fetch_source_rows_bright_sky(
    solar_wm2: Optional[float],
) -> list[dict[str, Any]]:
    """Raw Bright Sky GHI before the cache upsert."""
    return [
        _source_row(
            SOURCE_BRIGHT_SKY,
            _finite_nonneg(solar_wm2),
            None,
            None,
            source_identity=SOURCE_BRIGHT_SKY,
        )
    ]


def ensemble_compose_source_rows(
    sources: RadiationSources, ensemble: EnsembleRadiation
) -> list[dict[str, Any]]:
    """Source values actually consumed by one ensemble compose."""
    source_components = {
        SOURCE_IFS: (
            _finite_nonneg(sources.ghi_ifs),
            _finite_nonneg(sources.dni_ifs),
            _finite_nonneg(sources.dhi_ifs),
        ),
        SOURCE_ICON_D2: (
            _finite_nonneg(sources.ghi_icon_d2),
            _finite_nonneg(sources.dni_icon_d2),
            _finite_nonneg(sources.dhi_icon_d2),
        ),
        SOURCE_ICON_EU: (
            _finite_nonneg(sources.ghi_icon_eu),
            _finite_nonneg(sources.dni_icon_eu),
            _finite_nonneg(sources.dhi_icon_eu),
        ),
        SOURCE_BRIGHT_SKY: (_finite_nonneg(sources.ghi_bright_sky), None, None),
    }
    source_rows = []
    for source_name, (ghi, dni, dhi) in source_components.items():
        source_identity = source_name
        if source_name == SOURCE_IFS:
            source_identity = (
                str(sources.ifs_source_identity or "").strip()
                or SOURCE_IDENTITY_ECMWF_IFS
            )
        source_rows.append(
            _source_row(
                source_name,
                ghi,
                dni,
                dhi,
                ensemble.weights_used.get(source_name),
                source_identity=source_identity,
            )
        )
    return source_rows
