# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""POA beam helpers: horizontal direct to DNI, and beam IAM. @zara"""

from __future__ import annotations
import math
from typing import Optional

BEAM_MIN_ELEVATION_DEG = 3.0
DNI_MAX_WM2 = 1000.0
POA_DNI_IAM_FORMULA_VERSION = "poa_dni_iam_v1"
SOLAR_CONSTANT_MEINEL_WM2 = 1353.0
MEINEL_TRANSMITTANCE = 0.7
MEINEL_AIRMASS_EXPONENT = 0.678
KASTEN_YOUNG_COEFF = 0.50572
KASTEN_YOUNG_OFFSET_DEG = 6.07995
KASTEN_YOUNG_EXPONENT = -1.6364
HORIZONTAL_DIRECT_FRACTION = 0.85
HORIZONTAL_DIFFUSE_FRACTION = 0.15
MARTIN_RUIZ_A_R = 0.16


def _finite(value: Optional[float]) -> Optional[float]:
    if value is None:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    if not math.isfinite(number):
        return
    return number


def kasten_young_airmass(elevation_deg: float) -> float:
    """Relative optical airmass (Kasten-Young 1989)."""
    elevation = _finite(elevation_deg)
    if elevation is None:
        return math.inf
    sin_elevation = math.sin(math.radians(elevation))
    airmass_correction = (elevation + KASTEN_YOUNG_OFFSET_DEG) ** KASTEN_YOUNG_EXPONENT
    denominator = sin_elevation + KASTEN_YOUNG_COEFF * airmass_correction
    if denominator <= 0.0:
        return math.inf
    return 1.0 / denominator


def clear_sky_dni_meinel_wm2(elevation_deg: float) -> float:
    """Clear-sky direct normal irradiance, Meinel model (W/m²)."""
    airmass = kasten_young_airmass(elevation_deg)
    if not math.isfinite(airmass) or airmass <= 0.0:
        return 0.0
    return (
        SOLAR_CONSTANT_MEINEL_WM2
        * MEINEL_TRANSMITTANCE**airmass**MEINEL_AIRMASS_EXPONENT
    )


def horizontal_direct_to_dni(
    direct_h: Optional[float], elevation_deg: Optional[float]
) -> float:
    # fmt: off
    """Convert direct irradiance on the horizontal plane to DNI.

    ``direct_h`` is Open-Meteo ``direct_radiation`` (W/m² on the horizontal),
    not direct normal. Below ``BEAM_MIN_ELEVATION_DEG`` the beam is zero.
    The result is capped by the absolute ceiling and by the Meinel clear-sky
    DNI at this elevation.
    """
    # fmt: on
    direct = _finite(direct_h)
    elevation = _finite(elevation_deg)
    if direct is None or direct <= 0.0 or elevation is None:
        return 0.0
    if elevation < BEAM_MIN_ELEVATION_DEG:
        return 0.0
    sin_elevation = math.sin(math.radians(elevation))
    if sin_elevation <= 0.0:
        return 0.0
    dni_ceiling = min(DNI_MAX_WM2, clear_sky_dni_meinel_wm2(elevation))
    return min(dni_ceiling, direct / sin_elevation)


def beam_iam_martin_ruiz(aoi_deg: float, a_r: float = MARTIN_RUIZ_A_R) -> float:
    # fmt: off
    """Beam incidence-angle modifier (Martin-Ruiz).

    IAM is 1 at normal incidence and 0 at 90°. ``a_r`` is the angular
    loss coefficient.
    """
    # fmt: on
    if a_r <= 0.0:
        return 0.0
    angle = _finite(aoi_deg)
    if angle is None or angle <= 0.0:
        return 1.0
    if angle >= 90.0:
        return 0.0
    cos_angle = math.cos(math.radians(angle))
    if cos_angle <= 0.0:
        return 0.0
    denominator = 1.0 - math.exp(-1.0 / a_r)
    if denominator <= 0.0:
        return 0.0
    return (1.0 - math.exp(-cos_angle / a_r)) / denominator


def fill_horizontal_radiation(
    ghi: Optional[float], direct_h: Optional[float], diffuse_h: Optional[float]
) -> tuple[float, float, float]:
    # fmt: off
    """Fill a missing horizontal direct/diffuse split.

    A component at or below zero counts as missing. When both are missing,
    GHI is split by the clear-sky fractions. When one is present, the other
    is the residual of GHI.
    """
    # fmt: on
    ghi_number = _finite(ghi)
    ghi_value = ghi_number if ghi_number is not None and ghi_number > 0.0 else 0.0
    direct = _finite(direct_h)
    diffuse = _finite(diffuse_h)
    has_direct = direct is not None and direct > 0.0
    has_diffuse = diffuse is not None and diffuse > 0.0
    direct_value = direct if has_direct else 0.0
    diffuse_value = diffuse if has_diffuse else 0.0
    if ghi_value <= 0.0:
        return (0.0, direct_value, diffuse_value)
    if not has_direct and (not has_diffuse):
        direct_value = ghi_value * HORIZONTAL_DIRECT_FRACTION
        diffuse_value = ghi_value * HORIZONTAL_DIFFUSE_FRACTION
    elif not has_direct:
        direct_value = max(0.0, ghi_value - diffuse_value)
    elif not has_diffuse:
        diffuse_value = max(0.0, ghi_value - direct_value)
    return (ghi_value, direct_value, diffuse_value)


def poa_irradiance_components(
    ghi: Optional[float],
    direct_h: Optional[float],
    diffuse_h: Optional[float],
    elevation_deg: Optional[float],
) -> tuple[float, float, float]:
    # fmt: off
    """Return ``(ghi, dni, dhi)`` for ``IrradianceData``.

    ``direct_h`` stays horizontal until this boundary. Callers that already
    hold direct normal must not use this function.
    """
    # fmt: on
    ghi_value, direct_value, diffuse_value = fill_horizontal_radiation(
        ghi, direct_h, diffuse_h
    )
    dni = horizontal_direct_to_dni(direct_value, elevation_deg)
    return (ghi_value, dni, diffuse_value)


def erbs_diffuse_fraction(clearness: float) -> float:
    """Hourly diffuse fraction, Erbs, Klein and Duffie 1982."""
    clearness_value = max(0.0, clearness)
    if clearness_value <= 0.22:
        diffuse_fraction = 1.0 - 0.09 * clearness_value
    elif clearness_value <= 0.8:
        diffuse_fraction = (
            0.9511
            - 0.1604 * clearness_value
            + 4.388 * clearness_value * clearness_value
            - 16.638 * clearness_value**3
            + 12.336 * clearness_value**4
        )
    else:
        diffuse_fraction = 0.165
    return min(1.0, max(0.0, diffuse_fraction))


def extraterrestrial_horizontal(elevation_deg: float, day_of_year: int) -> float:
    """Horizontal extraterrestrial irradiance for one hour's solar elevation."""
    if elevation_deg <= 0.0:
        return 0.0
    distance_correction = 1.0 + 0.033 * math.cos(2.0 * math.pi * day_of_year / 365.0)
    return 1367.0 * distance_correction * math.sin(math.radians(elevation_deg))


def erbs_split(
    ghi: float, elevation_deg: float, day_of_year: int
) -> tuple[float, float, float]:
    # fmt: off
    """Split a scalar GHI into ``(ghi, direct_h, diffuse_h)``.

    Callers that already hold direct and diffuse must not call this.
    """
    # fmt: on
    extraterrestrial_ghi = extraterrestrial_horizontal(elevation_deg, day_of_year)
    if ghi <= 0.0 or extraterrestrial_ghi <= 1.0:
        return (ghi, 0.0, max(ghi, 0.0))
    diffuse = erbs_diffuse_fraction(ghi / extraterrestrial_ghi) * ghi
    return (ghi, max(0.0, ghi - diffuse), diffuse)
