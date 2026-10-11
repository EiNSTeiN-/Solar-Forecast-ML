# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""POA beam helpers: horizontal direct to DNI, and beam IAM. @zara"""
from __future__ import annotations
import math
from typing import Optional
BEAM_MIN_ELEVATION_DEG = 3.0
DNI_MAX_WM2 = 1000.0
POA_DNI_IAM_FORMULA_VERSION = 'poa_dni_iam_v1'
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
        _var_var_0 = float(value)
    except (TypeError, ValueError):
        return
    if not math.isfinite(_var_var_0):
        return
    return _var_var_0

def kasten_young_airmass(elevation_deg: float) -> float:
    """Relative optical airmass (Kasten-Young 1989)."""
    _var_var_1 = _finite(elevation_deg)
    if _var_var_1 is None:
        return math.inf
    _var_var_2 = math.sin(math.radians(_var_var_1))
    _var_var_3 = (_var_var_1 + KASTEN_YOUNG_OFFSET_DEG) ** KASTEN_YOUNG_EXPONENT
    _var_var_4 = _var_var_2 + KASTEN_YOUNG_COEFF * _var_var_3
    if _var_var_4 <= 0.0:
        return math.inf
    return 1.0 / _var_var_4

def clear_sky_dni_meinel_wm2(elevation_deg: float) -> float:
    """Clear-sky direct normal irradiance, Meinel model (W/m²)."""
    _var_var_5 = kasten_young_airmass(elevation_deg)
    if not math.isfinite(_var_var_5) or _var_var_5 <= 0.0:
        return 0.0
    return SOLAR_CONSTANT_MEINEL_WM2 * MEINEL_TRANSMITTANCE ** _var_var_5 ** MEINEL_AIRMASS_EXPONENT

def horizontal_direct_to_dni(direct_h: Optional[float], elevation_deg: Optional[float]) -> float:
    """Convert direct irradiance on the horizontal plane to DNI.

    ``direct_h`` is Open-Meteo ``direct_radiation`` (W/m² on the horizontal),
    not direct normal. Below ``BEAM_MIN_ELEVATION_DEG`` the beam is zero.
    The result is capped by the absolute ceiling and by the Meinel clear-sky
    DNI at this elevation.
    """
    _var_var_6 = _finite(direct_h)
    _var_var_1 = _finite(elevation_deg)
    if _var_var_6 is None or _var_var_6 <= 0.0 or _var_var_1 is None:
        return 0.0
    if _var_var_1 < BEAM_MIN_ELEVATION_DEG:
        return 0.0
    _var_var_2 = math.sin(math.radians(_var_var_1))
    if _var_var_2 <= 0.0:
        return 0.0
    _var_var_7 = min(DNI_MAX_WM2, clear_sky_dni_meinel_wm2(_var_var_1))
    return min(_var_var_7, _var_var_6 / _var_var_2)

def beam_iam_martin_ruiz(aoi_deg: float, a_r: float=MARTIN_RUIZ_A_R) -> float:
    """Beam incidence-angle modifier (Martin-Ruiz).

    IAM is 1 at normal incidence and 0 at 90°. ``a_r`` is the angular
    loss coefficient.
    """
    if a_r <= 0.0:
        return 0.0
    _var_var_8 = _finite(aoi_deg)
    if _var_var_8 is None or _var_var_8 <= 0.0:
        return 1.0
    if _var_var_8 >= 90.0:
        return 0.0
    _var_var_9 = math.cos(math.radians(_var_var_8))
    if _var_var_9 <= 0.0:
        return 0.0
    _var_var_4 = 1.0 - math.exp(-1.0 / a_r)
    if _var_var_4 <= 0.0:
        return 0.0
    return (1.0 - math.exp(-_var_var_9 / a_r)) / _var_var_4

def fill_horizontal_radiation(ghi: Optional[float], direct_h: Optional[float], diffuse_h: Optional[float]) -> tuple[float, float, float]:
    """Fill a missing horizontal direct/diffuse split.

    A component at or below zero counts as missing. When both are missing,
    GHI is split by the clear-sky fractions. When one is present, the other
    is the residual of GHI.
    """
    _var_var_10 = _finite(ghi)
    _var_var_11 = _var_var_10 if _var_var_10 is not None and _var_var_10 > 0.0 else 0.0
    _var_var_6 = _finite(direct_h)
    _var_var_12 = _finite(diffuse_h)
    _var_var_13 = _var_var_6 is not None and _var_var_6 > 0.0
    _var_var_14 = _var_var_12 is not None and _var_var_12 > 0.0
    _var_var_15 = _var_var_6 if _var_var_13 else 0.0
    _var_var_16 = _var_var_12 if _var_var_14 else 0.0
    if _var_var_11 <= 0.0:
        return (0.0, _var_var_15, _var_var_16)
    if not _var_var_13 and (not _var_var_14):
        _var_var_15 = _var_var_11 * HORIZONTAL_DIRECT_FRACTION
        _var_var_16 = _var_var_11 * HORIZONTAL_DIFFUSE_FRACTION
    elif not _var_var_13:
        _var_var_15 = max(0.0, _var_var_11 - _var_var_16)
    elif not _var_var_14:
        _var_var_16 = max(0.0, _var_var_11 - _var_var_15)
    return (_var_var_11, _var_var_15, _var_var_16)

def poa_irradiance_components(ghi: Optional[float], direct_h: Optional[float], diffuse_h: Optional[float], elevation_deg: Optional[float]) -> tuple[float, float, float]:
    """Return ``(ghi, dni, dhi)`` for ``IrradianceData``.

    ``direct_h`` stays horizontal until this boundary. Callers that already
    hold direct normal must not use this function.
    """
    _var_var_11, _var_var_15, _var_var_16 = fill_horizontal_radiation(ghi, direct_h, diffuse_h)
    _var_var_17 = horizontal_direct_to_dni(_var_var_15, elevation_deg)
    return (_var_var_11, _var_var_17, _var_var_16)

def erbs_diffuse_fraction(clearness: float) -> float:
    """Hourly diffuse fraction, Erbs, Klein and Duffie 1982."""
    _var_var_18 = max(0.0, clearness)
    if _var_var_18 <= 0.22:
        _var_var_19 = 1.0 - 0.09 * _var_var_18
    elif _var_var_18 <= 0.8:
        _var_var_19 = 0.9511 - 0.1604 * _var_var_18 + 4.388 * _var_var_18 * _var_var_18 - 16.638 * _var_var_18 ** 3 + 12.336 * _var_var_18 ** 4
    else:
        _var_var_19 = 0.165
    return min(1.0, max(0.0, _var_var_19))

def extraterrestrial_horizontal(elevation_deg: float, day_of_year: int) -> float:
    """Horizontal extraterrestrial irradiance for one hour's solar elevation."""
    if elevation_deg <= 0.0:
        return 0.0
    _var_var_20 = 1.0 + 0.033 * math.cos(2.0 * math.pi * day_of_year / 365.0)
    return 1367.0 * _var_var_20 * math.sin(math.radians(elevation_deg))

def erbs_split(ghi: float, elevation_deg: float, day_of_year: int) -> tuple[float, float, float]:
    """Split a scalar GHI into ``(ghi, direct_h, diffuse_h)``.

    Callers that already hold direct and diffuse must not call this.
    """
    _var_var_21 = extraterrestrial_horizontal(elevation_deg, day_of_year)
    if ghi <= 0.0 or _var_var_21 <= 1.0:
        return (ghi, 0.0, max(ghi, 0.0))
    _var_var_12 = erbs_diffuse_fraction(ghi / _var_var_21) * ghi
    return (ghi, max(0.0, ghi - _var_var_12), _var_var_12)
