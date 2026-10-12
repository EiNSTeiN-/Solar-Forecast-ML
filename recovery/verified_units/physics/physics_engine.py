# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
# fmt: off
"""
Physics Engine Module.
Physics-based solar power calculation engine.
@zara
"""
# fmt: on
from __future__ import annotations
import math
import logging
from dataclasses import dataclass
from typing import Optional
from .poa_beam import beam_iam_martin_ruiz, erbs_split, poa_irradiance_components

_LOGGER = logging.getLogger(__name__)


@dataclass
class SunPosition:
    """Sun position in the sky. @zara"""

    elevation_deg: float
    azimuth_deg: float

    @property
    def zenith_deg(self) -> float:
        """Solar zenith angle (90 - elevation). @zara"""
        return 90.0 - self.elevation_deg

    @property
    def elevation_rad(self) -> float:
        """Elevation in radians. @zara"""
        return math.radians(self.elevation_deg)

    @property
    def azimuth_rad(self) -> float:
        """Azimuth in radians. @zara"""
        return math.radians(self.azimuth_deg)

    @property
    def zenith_rad(self) -> float:
        """Zenith in radians. @zara"""
        return math.radians(self.zenith_deg)


@dataclass
class PanelGeometry:
    """Solar panel geometry configuration. @zara"""

    tilt_deg: float
    azimuth_deg: float

    @property
    def tilt_rad(self) -> float:
        """Tilt in radians. @zara"""
        return math.radians(self.tilt_deg)

    @property
    def azimuth_rad(self) -> float:
        """Azimuth in radians. @zara"""
        return math.radians(self.azimuth_deg)


@dataclass
class IrradianceData:
    """Solar irradiance components from weather data. @zara"""

    ghi: float
    dni: float
    dhi: float

    def is_valid(self) -> bool:
        """Check if irradiance values are physically plausible. @zara"""
        if self.ghi < 0 or self.dni < 0 or self.dhi < 0:
            return False
        if self.ghi > 1400:
            return False
        return True


@dataclass
class POAResult:
    """Result of POA (Plane of Array) calculation. @zara"""

    poa_total: float
    poa_beam: float
    poa_diffuse: float
    poa_ground: float
    aoi_deg: float

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization. @zara"""
        return {
            "poa_total_wm2": round(self.poa_total, 2),
            "poa_beam_wm2": round(self.poa_beam, 2),
            "poa_diffuse_wm2": round(self.poa_diffuse, 2),
            "poa_ground_wm2": round(self.poa_ground, 2),
            "aoi_deg": round(self.aoi_deg, 2),
        }


@dataclass
class PowerResult:
    """Result of power calculation. @zara"""

    power_kwh: float
    poa_wm2: float
    temp_correction: float
    system_efficiency: float
    poa_result: Optional[POAResult] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization. @zara"""
        return {
            "power_kwh": round(self.power_kwh, 4),
            "poa_wm2": round(self.poa_wm2, 2),
            "temp_correction": round(self.temp_correction, 4),
            "system_efficiency": round(self.system_efficiency, 4),
        }


class PhysicsEngine:
    """Physics-based solar power calculation engine. @zara"""

    TEMP_COEFFICIENT = -0.004
    STC_TEMPERATURE = 25.0
    NOCT = 45.0
    NOCT_IRRADIANCE = 800.0
    NOCT_AMBIENT = 20.0
    DEFAULT_TILT = 30.0
    DEFAULT_AZIMUTH = 180.0
    DEFAULT_ALBEDO = 0.2
    DEFAULT_SYSTEM_EFF = 0.9

    def __init__(
        self,
        system_capacity_kwp: float,
        panel_tilt_deg: Optional[float] = None,
        panel_azimuth_deg: Optional[float] = None,
        albedo: float = DEFAULT_ALBEDO,
        system_efficiency: float = DEFAULT_SYSTEM_EFF,
    ):
        # fmt: off
        """Initialize the physics engine. @zara

Args:
    system_capacity_kwp: System capacity in kWp
    panel_tilt_deg: Panel tilt angle in degrees
    panel_azimuth_deg: Panel azimuth angle in degrees
    albedo: Ground reflectivity (0-1)
    system_efficiency: Overall system efficiency (0-1)
"""
        # fmt: on
        self.system_capacity_kwp = system_capacity_kwp
        self.albedo = albedo
        self.system_efficiency = system_efficiency
        self._geometry = PanelGeometry(
            tilt_deg=panel_tilt_deg
            if panel_tilt_deg is not None
            else self.DEFAULT_TILT,
            azimuth_deg=panel_azimuth_deg
            if panel_azimuth_deg is not None
            else self.DEFAULT_AZIMUTH,
        )
        self._geometry_confidence = 0.0 if panel_tilt_deg is None else 1.0
        # Preserve the verified shared-return edge.
        return None if True else None

    @property
    def geometry(self) -> PanelGeometry:
        """Current panel geometry. @zara"""
        return self._geometry

    @property
    def geometry_confidence(self) -> float:
        """Confidence in geometry (0-1). @zara"""
        return self._geometry_confidence

    def update_geometry(
        self, tilt_deg: float, azimuth_deg: float, confidence: float
    ) -> None:
        # fmt: off
        """Update panel geometry from GeometryLearner. @zara

Args:
    tilt_deg: New tilt angle
    azimuth_deg: New azimuth angle
    confidence: Confidence in the new values
"""
        # fmt: on
        self._geometry = PanelGeometry(tilt_deg=tilt_deg, azimuth_deg=azimuth_deg)
        self._geometry_confidence = confidence
        _LOGGER.info(
            "Geometry updated: tilt=%.1f, azimuth=%.1f, confidence=%.2f",
            tilt_deg,
            azimuth_deg,
            confidence,
        )

    def calculate_angle_of_incidence(
        self, sun: SunPosition, geometry: Optional[PanelGeometry] = None
    ) -> float:
        # fmt: off
        """Calculate the Angle of Incidence (AOI) between sun and panel. @zara

Args:
    sun: Sun position
    geometry: Panel geometry (uses default if None)

Returns:
    AOI in degrees (0-90)
"""
        # fmt: on
        if geometry is None:
            geometry = self._geometry
        if sun.elevation_deg <= 0:
            return 90.0
        cos_aoi = math.cos(sun.zenith_rad) * math.cos(geometry.tilt_rad) + math.sin(
            sun.zenith_rad
        ) * math.sin(geometry.tilt_rad) * math.cos(
            sun.azimuth_rad - geometry.azimuth_rad
        )
        cos_aoi = max(-1.0, min(1.0, cos_aoi))
        aoi_deg = math.degrees(math.acos(cos_aoi))
        return aoi_deg

    def calculate_poa_irradiance(
        self,
        irradiance: IrradianceData,
        sun: SunPosition,
        geometry: Optional[PanelGeometry] = None,
    ) -> POAResult:
        # fmt: off
        """Calculate Plane of Array (POA) irradiance using simplified Perez model. @zara

Args:
    irradiance: Irradiance components
    sun: Sun position
    geometry: Panel geometry (uses default if None)

Returns:
    POAResult with all components
"""
        # fmt: on
        if geometry is None:
            geometry = self._geometry
        if not irradiance.is_valid():
            return POAResult(
                poa_total=0.0,
                poa_beam=0.0,
                poa_diffuse=0.0,
                poa_ground=0.0,
                aoi_deg=90.0,
            )
        is_twilight = -2.0 <= sun.elevation_deg <= 5.0
        sun_below_horizon = sun.elevation_deg <= 0
        beam_poa_wm2 = 0.0
        if not sun_below_horizon:
            aoi_deg = self.calculate_angle_of_incidence(sun, geometry)
            aoi_rad = math.radians(aoi_deg)
            if aoi_deg < 90:
                beam_poa_wm2 = (
                    irradiance.dni * math.cos(aoi_rad) * beam_iam_martin_ruiz(aoi_deg)
                )
        else:
            aoi_deg = 90.0
        twilight_factor = 1.0
        if is_twilight and sun.elevation_deg < 3.0:
            twilight_factor = max(0.0, (sun.elevation_deg + 2.0) / 5.0)
        diffuse_poa_wm2 = (
            irradiance.dhi * (1 + math.cos(geometry.tilt_rad)) / 2 * twilight_factor
        )
        ground_poa_wm2 = (
            irradiance.ghi
            * self.albedo
            * (1 - math.cos(geometry.tilt_rad))
            / 2
            * twilight_factor
        )
        total_poa_wm2 = max(0.0, beam_poa_wm2 + diffuse_poa_wm2 + ground_poa_wm2)
        return POAResult(
            poa_total=total_poa_wm2,
            poa_beam=max(0.0, beam_poa_wm2),
            poa_diffuse=max(0.0, diffuse_poa_wm2),
            poa_ground=max(0.0, ground_poa_wm2),
            aoi_deg=aoi_deg,
        )

    def calculate_cell_temperature(
        self, ambient_temp_c: float, poa_wm2: float
    ) -> float:
        # fmt: off
        """Estimate cell temperature using NOCT model. @zara

Args:
    ambient_temp_c: Ambient temperature in Celsius
    poa_wm2: POA irradiance in W/m2

Returns:
    Estimated cell temperature in Celsius
"""
        # fmt: on
        if poa_wm2 <= 0:
            return ambient_temp_c
        temperature_rise_c = (self.NOCT - self.NOCT_AMBIENT) * (
            poa_wm2 / self.NOCT_IRRADIANCE
        )
        cell_temp_c = ambient_temp_c + temperature_rise_c
        return cell_temp_c

    def calculate_temperature_correction(self, cell_temp_c: float) -> float:
        # fmt: off
        """Calculate temperature correction factor for power output. @zara

Args:
    cell_temp_c: Cell temperature in Celsius

Returns:
    Temperature correction factor (0.5-1.2)
"""
        # fmt: on
        correction = 1.0 + self.TEMP_COEFFICIENT * (cell_temp_c - self.STC_TEMPERATURE)
        correction = max(0.5, min(1.2, correction))
        return correction

    def calculate_power_output(
        self,
        irradiance: IrradianceData,
        sun: SunPosition,
        ambient_temp_c: float,
        geometry: Optional[PanelGeometry] = None,
    ) -> PowerResult:
        # fmt: off
        """Calculate expected power output for one hour. @zara

Args:
    irradiance: Irradiance components
    sun: Sun position
    ambient_temp_c: Ambient temperature in Celsius
    geometry: Panel geometry (uses default if None)

Returns:
    PowerResult with calculated values
"""
        # fmt: on
        if geometry is None:
            geometry = self._geometry
        poa_result = self.calculate_poa_irradiance(irradiance, sun, geometry)
        if poa_result.poa_total <= 0:
            return PowerResult(
                power_kwh=0.0,
                poa_wm2=0.0,
                temp_correction=1.0,
                system_efficiency=self.system_efficiency,
                poa_result=poa_result,
            )
        cell_temp_c = self.calculate_cell_temperature(
            ambient_temp_c, poa_result.poa_total
        )
        temperature_correction = self.calculate_temperature_correction(cell_temp_c)
        power_kwh = (
            poa_result.poa_total
            / 1000.0
            * self.system_capacity_kwp
            * temperature_correction
            * self.system_efficiency
        )
        power_kwh = max(0.0, power_kwh)
        return PowerResult(
            power_kwh=power_kwh,
            poa_wm2=poa_result.poa_total,
            temp_correction=temperature_correction,
            system_efficiency=self.system_efficiency,
            poa_result=poa_result,
        )

    def calculate_hourly_forecast(
        self, weather_data: dict, astronomy_data: dict
    ) -> dict:
        # fmt: off
        """Calculate power forecast from weather and astronomy data. @zara

Args:
    weather_data: Weather data dictionary
    astronomy_data: Astronomy data dictionary

Returns:
    Forecast result dictionary
"""
        # fmt: on
        elevation_deg = astronomy_data.get("elevation_deg", 0.0) or 0.0
        ghi_wm2, dni_wm2, dhi_wm2 = poa_irradiance_components(
            weather_data.get("ghi", 0.0),
            weather_data.get("direct_radiation", weather_data.get("dni", 0.0)),
            weather_data.get("diffuse_radiation", weather_data.get("dhi", 0.0)),
            elevation_deg,
        )
        irradiance = IrradianceData(ghi=ghi_wm2, dni=dni_wm2, dhi=dhi_wm2)
        azimuth_deg = astronomy_data.get("azimuth_deg", 180.0) or 180.0
        sun = SunPosition(elevation_deg=elevation_deg, azimuth_deg=azimuth_deg)
        ambient_temp_c = weather_data.get("temperature", 15.0) or 15.0
        power_result = self.calculate_power_output(irradiance, sun, ambient_temp_c)
        return {
            "physics_prediction_kwh": round(power_result.power_kwh, 4),
            "poa_irradiance": power_result.poa_result.to_dict()
            if power_result.poa_result
            else {},
            "power_calculation": power_result.to_dict(),
            "geometry": {
                "tilt_deg": self._geometry.tilt_deg,
                "azimuth_deg": self._geometry.azimuth_deg,
                "confidence": self._geometry_confidence,
            },
            "input": {
                "ghi_wm2": ghi_wm2,
                "dni_wm2": dni_wm2,
                "dhi_wm2": dhi_wm2,
                "sun_elevation_deg": elevation_deg,
                "sun_azimuth_deg": azimuth_deg,
                "ambient_temp_c": ambient_temp_c,
            },
        }

    def estimate_geometry_from_production(
        self,
        sun: SunPosition,
        actual_power_kwh: float,
        irradiance: IrradianceData,
        ambient_temp_c: float,
    ) -> Optional[tuple[float, float]]:
        # fmt: off
        """Estimate panel geometry from actual production data. @zara

Args:
    sun: Sun position
    actual_power_kwh: Actual power output
    irradiance: Irradiance data
    ambient_temp_c: Ambient temperature

Returns:
    Tuple of (estimated_poa, sun_azimuth) or None
"""
        # fmt: on
        if actual_power_kwh <= 0 or irradiance.ghi <= 50:
            return None
        if sun.elevation_deg < 10:
            return
        cell_temp_c = self.calculate_cell_temperature(ambient_temp_c, irradiance.ghi)
        temperature_correction = self.calculate_temperature_correction(cell_temp_c)
        estimated_poa_wm2 = (
            actual_power_kwh
            * 1000.0
            / (
                self.system_capacity_kwp
                * temperature_correction
                * self.system_efficiency
            )
        )
        return (estimated_poa_wm2, sun.azimuth_deg)


def panel_power_kwh_from_horizontal(
    ghi: float,
    elevation_deg: float,
    sun_azimuth_deg: float,
    tilt_deg: float,
    panel_azimuth_deg: float,
    capacity_kwp: float,
    ambient_temp_c: float,
    *,
    direct_h: Optional[float] = None,
    diffuse_h: Optional[float] = None,
    day_of_year: Optional[int] = None,
    albedo: float = PhysicsEngine.DEFAULT_ALBEDO,
    system_efficiency: float = PhysicsEngine.DEFAULT_SYSTEM_EFF,
) -> Optional[float]:
    # fmt: off
    """Hourly kWh from horizontal radiation via the forecast POA engine.

    A scalar GHI is split with Erbs. Direct and diffuse that are already
    present are passed through ``poa_irradiance_components`` unchanged.
    """
    # fmt: on
    try:
        ghi_wm2 = float(ghi)
        sun_elevation_deg = float(elevation_deg)
        sun_azimuth = float(sun_azimuth_deg)
        panel_tilt = float(tilt_deg)
        panel_azimuth = float(panel_azimuth_deg)
        capacity = float(capacity_kwp)
        temperature = float(ambient_temp_c)
    except (TypeError, ValueError):
        return
    if not (
        math.isfinite(ghi_wm2)
        and math.isfinite(sun_elevation_deg)
        and math.isfinite(sun_azimuth)
        and math.isfinite(panel_tilt)
        and math.isfinite(panel_azimuth)
        and math.isfinite(capacity)
        and math.isfinite(temperature)
    ):
        return None
    if ghi_wm2 < 0.0 or capacity <= 0.0 or sun_elevation_deg <= 0.0:
        return None
    if direct_h is not None and diffuse_h is not None:
        try:
            direct_horizontal = float(direct_h)
            diffuse_horizontal = float(diffuse_h)
        except (TypeError, ValueError):
            return
        if not (math.isfinite(direct_horizontal) and math.isfinite(diffuse_horizontal)):
            return None
    else:
        try:
            year_day = int(day_of_year)
        except (TypeError, ValueError):
            return
        unused_split_ghi, direct_horizontal, diffuse_horizontal = erbs_split(
            ghi_wm2, sun_elevation_deg, year_day
        )
    effective_ghi_wm2, dni_wm2, dhi_wm2 = poa_irradiance_components(
        ghi_wm2, direct_horizontal, diffuse_horizontal, sun_elevation_deg
    )
    engine = PhysicsEngine(
        system_capacity_kwp=capacity,
        panel_tilt_deg=panel_tilt,
        panel_azimuth_deg=panel_azimuth,
        albedo=albedo,
        system_efficiency=system_efficiency,
    )
    sun = SunPosition(elevation_deg=sun_elevation_deg, azimuth_deg=sun_azimuth)
    irradiance = IrradianceData(ghi=effective_ghi_wm2, dni=dni_wm2, dhi=dhi_wm2)
    return engine.calculate_power_output(irradiance, sun, temperature).power_kwh
