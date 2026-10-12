# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
# fmt: off
"""
Panel Group Calculator Module.
Calculator for multi-group solar panel systems.
@zara
"""
# fmt: on
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from ..data.db_manager import DatabaseManager
from .physics_engine import (
    IrradianceData,
    PanelGeometry,
    PhysicsEngine,
    POAResult,
    SunPosition,
)
from .poa_beam import poa_irradiance_components
from .physics_calibrator import PhysicsCalibrator

_LOGGER = logging.getLogger(__name__)


@dataclass
class PanelGroup:
    """Represents a group of solar panels with the same orientation. @zara"""

    name: str
    power_wp: float
    azimuth_deg: float
    tilt_deg: float
    power_sensor: Optional[str] = None

    @property
    def power_kwp(self) -> float:
        """Power in kWp. @zara"""
        return self.power_wp / 1000.0

    @property
    def geometry(self) -> PanelGeometry:
        """Get panel geometry for this group. @zara"""
        return PanelGeometry(tilt_deg=self.tilt_deg, azimuth_deg=self.azimuth_deg)

    @property
    def has_power_sensor(self) -> bool:
        """Check if this group has a power sensor configured. @zara"""
        return self.power_sensor is not None and len(self.power_sensor) > 0

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization. @zara"""
        data = {
            "name": self.name,
            "power_wp": self.power_wp,
            "power_kwp": round(self.power_kwp, 3),
            "azimuth_deg": self.azimuth_deg,
            "tilt_deg": self.tilt_deg,
        }
        if self.power_sensor:
            data["power_sensor"] = self.power_sensor
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "PanelGroup":
        """Create PanelGroup from dictionary. @zara"""
        return cls(
            name=data.get("name", "Unknown"),
            power_wp=float(data.get("power_wp", 0)),
            azimuth_deg=float(data.get("azimuth", 180)),
            tilt_deg=float(data.get("tilt", 30)),
            power_sensor=data.get("power_sensor"),
        )


@dataclass
class PanelGroupResult:
    """Result of power calculation for a single panel group. @zara"""

    group: PanelGroup
    power_kwh: float
    poa_result: POAResult
    raw_power_kwh: float = 0.0
    calibration_factor: float = 1.0
    contribution_percent: float = 0.0
    actual_kwh: Optional[float] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization. @zara"""
        data = {
            "group_name": self.group.name,
            "power_wp": self.group.power_wp,
            "azimuth_deg": self.group.azimuth_deg,
            "tilt_deg": self.group.tilt_deg,
            "power_kwh": round(self.power_kwh, 4),
            "raw_power_kwh": round(self.raw_power_kwh, 4),
            "calibration_factor": round(self.calibration_factor, 4),
            "contribution_percent": round(self.contribution_percent, 1),
            "poa_total_wm2": round(self.poa_result.poa_total, 2),
            "aoi_deg": round(self.poa_result.aoi_deg, 1),
        }
        if self.group.power_sensor:
            data["power_sensor"] = self.group.power_sensor
        if self.actual_kwh is not None:
            data["actual_kwh"] = round(self.actual_kwh, 4)
        return data


@dataclass
class MultiGroupResult:
    """Combined result of power calculation for all panel groups. @zara"""

    total_power_kwh: float
    group_results: List[PanelGroupResult] = field(default_factory=list)
    total_capacity_kwp: float = 0.0

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization. @zara"""
        return {
            "total_power_kwh": round(self.total_power_kwh, 4),
            "total_capacity_kwp": round(self.total_capacity_kwp, 3),
            "group_count": len(self.group_results),
            "groups": [group_result.to_dict() for group_result in self.group_results],
        }


class PanelGroupCalculator:
    # fmt: off
    """Calculator for multi-group solar panel systems. @zara

    Handles calculation of power output for solar systems with multiple
    panel groups that have different orientations.
    """
    # fmt: on
    TEMP_COEFFICIENT = -0.004
    STC_TEMPERATURE = 25.0
    DEFAULT_ALBEDO = 0.2
    DEFAULT_SYSTEM_EFF = 0.9

    def __init__(
        self,
        panel_groups: Optional[List[PanelGroup] | List[dict]] = None,
        albedo: Optional[float] = None,
        system_efficiency: Optional[float] = None,
        db_manager: Optional[DatabaseManager] = None,
    ):
        # fmt: off
        """Initialize the panel group calculator. @zara

Args:
    panel_groups: List of PanelGroup objects or dicts with group config
    albedo: Ground reflectivity (default 0.2)
    system_efficiency: Overall system efficiency (default 0.90)
    db_manager: Database manager for loading config
"""
        # fmt: on
        self._db = db_manager
        self.albedo = albedo if albedo is not None else self.DEFAULT_ALBEDO
        self.system_efficiency = (
            system_efficiency
            if system_efficiency is not None
            else self.DEFAULT_SYSTEM_EFF
        )
        self._groups = []
        self._calibrator = None
        self._calibration_enabled = True
        if panel_groups:
            self.set_panel_groups(panel_groups)
        _LOGGER.debug(
            "PanelGroupCalculator initialized with %d groups, total %.2f kWp, albedo=%.2f, sys_eff=%.2f",
            len(self._groups),
            self.total_capacity_kwp,
            self.albedo,
            self.system_efficiency,
        )

    async def async_init(self) -> None:
        """Async initialization - load physics config from DB. @zara"""
        if not self._db:
            return
        try:
            config_row = await self._db.fetchone(
                "SELECT albedo, system_efficiency FROM physics_learning_config WHERE id = 1"
            )
            if config_row:
                self.albedo = config_row[0] if config_row[0] else self.DEFAULT_ALBEDO
                self.system_efficiency = (
                    config_row[1] if config_row[1] else self.DEFAULT_SYSTEM_EFF
                )
                _LOGGER.debug(
                    "Loaded physics config from DB: albedo=%.2f, system_eff=%.2f",
                    self.albedo,
                    self.system_efficiency,
                )
        except Exception as error:
            _LOGGER.debug(f"Could not load physics config from DB: {error}")
        # Folded expression preserves the verified shared-return edge.
        return None if True else None

    def set_panel_groups(self, panel_groups: List[PanelGroup] | List[dict]) -> None:
        """Set or update panel groups configuration. @zara"""
        self._groups = []
        for group_index, group in enumerate(panel_groups):
            if isinstance(group, PanelGroup):
                self._groups.append(group)
            elif isinstance(group, dict):
                self._groups.append(
                    PanelGroup(
                        name=group.get("name", f"Group {group_index + 1}"),
                        power_wp=float(group.get("power_wp", 0)),
                        azimuth_deg=float(group.get("azimuth", 180)),
                        tilt_deg=float(group.get("tilt", 30)),
                        power_sensor=group.get("power_sensor"),
                    )
                )
        _LOGGER.debug(
            "Panel groups updated: %d groups, total %.2f kWp",
            len(self._groups),
            self.total_capacity_kwp,
        )

    @property
    def groups(self) -> List[PanelGroup]:
        """Get all panel groups. @zara"""
        return self._groups

    @property
    def group_count(self) -> int:
        """Get number of panel groups. @zara"""
        return len(self._groups)

    @property
    def total_capacity_wp(self) -> float:
        """Total system capacity in Watts peak. @zara"""
        return sum((group.power_wp for group in self._groups))

    @property
    def total_capacity_kwp(self) -> float:
        """Total system capacity in kWp. @zara"""
        return self.total_capacity_wp / 1000.0

    @property
    def has_groups(self) -> bool:
        """Check if panel groups are configured. @zara"""
        return len(self._groups) > 0

    def set_calibrator(self, calibrator: PhysicsCalibrator) -> None:
        """Set the physics calibrator for self-learning corrections. @zara"""
        self._calibrator = calibrator
        _LOGGER.debug("PhysicsCalibrator attached to PanelGroupCalculator")

    def get_calibration_factor(
        self,
        group_name: str,
        hour: Optional[int] = None,
        cloud_cover: Optional[float] = None,
        sun_elevation: Optional[float] = None,
        visibility_m: Optional[float] = None,
        precipitation_mm: Optional[float] = None,
        temperature_c: Optional[float] = None,
        snow_covered_panels: bool = False,
        frost_detected: Optional[str] = None,
        frost_score: Optional[int] = None,
        ghi_ratio: Optional[float] = None,
    ) -> float:
        # fmt: off
        """Get calibration correction factor for a group. @zara

Args:
    group_name: Name of the panel group
    hour: Optional hour for hourly-specific factor
    cloud_cover: Optional cloud cover (0-100)
    sun_elevation: Optional sun elevation (degrees)
    visibility_m: Optional visibility in meters
    precipitation_mm: Optional precipitation in mm/h
    temperature_c: Optional temperature in Celsius
    snow_covered_panels: Whether panels are snow-covered
    frost_detected: Optional frost detection status
    frost_score: Optional frost score 0-10
    ghi_ratio: Optional GHI/clear_sky ratio for bucket override

Returns:
    Correction factor (1.0 = no correction)
"""
        # fmt: on
        if not self._calibration_enabled or self._calibrator is None:
            return 1.0
        return self._calibrator.get_correction_factor(
            group_name=group_name,
            hour=hour,
            cloud_cover=cloud_cover,
            sun_elevation=sun_elevation,
            visibility_m=visibility_m,
            precipitation_mm=precipitation_mm,
            temperature_c=temperature_c,
            snow_covered_panels=snow_covered_panels,
            frost_detected=frost_detected,
            frost_score=frost_score,
            ghi_ratio=ghi_ratio,
        )

    def calculate_group_power(
        self,
        group: PanelGroup,
        irradiance: IrradianceData,
        sun: SunPosition,
        ambient_temp_c: float,
        hour: Optional[int] = None,
        cloud_cover: Optional[float] = None,
        visibility_m: Optional[float] = None,
        precipitation_mm: Optional[float] = None,
        temperature_c: Optional[float] = None,
        snow_covered_panels: bool = False,
        frost_detected: Optional[str] = None,
        frost_score: Optional[int] = None,
        ghi_ratio: Optional[float] = None,
    ) -> PanelGroupResult:
        # fmt: off
        """Calculate power output for a single panel group with calibration. @zara

Args:
    group: The panel group to calculate for
    irradiance: Solar irradiance components (GHI, DNI, DHI)
    sun: Current sun position
    ambient_temp_c: Ambient temperature in Celsius
    hour: Optional hour for hourly-specific calibration factor
    cloud_cover: Optional cloud cover (0-100)
    visibility_m: Optional visibility in meters
    precipitation_mm: Optional precipitation in mm/h
    temperature_c: Optional temperature in Celsius
    snow_covered_panels: Whether panels are snow-covered
    frost_detected: Optional frost detection status
    frost_score: Optional frost score 0-10

Returns:
    PanelGroupResult with power output and details
"""
        # fmt: on
        engine = PhysicsEngine(
            system_capacity_kwp=group.power_kwp,
            panel_tilt_deg=group.tilt_deg,
            panel_azimuth_deg=group.azimuth_deg,
            albedo=self.albedo,
            system_efficiency=self.system_efficiency,
        )
        poa_result = engine.calculate_poa_irradiance(irradiance, sun, group.geometry)
        power_result = engine.calculate_power_output(
            irradiance, sun, ambient_temp_c, group.geometry
        )
        raw_power_kwh = power_result.power_kwh
        calibration_factor = self.get_calibration_factor(
            group.name,
            hour,
            cloud_cover,
            sun.elevation_deg,
            visibility_m,
            precipitation_mm,
            temperature_c,
            snow_covered_panels,
            frost_detected,
            frost_score,
            ghi_ratio,
        )
        calibrated_power_kwh = raw_power_kwh * calibration_factor
        return PanelGroupResult(
            group=group,
            power_kwh=calibrated_power_kwh,
            poa_result=poa_result,
            raw_power_kwh=raw_power_kwh,
            calibration_factor=calibration_factor,
        )

    def calculate_total_power(
        self,
        irradiance: IrradianceData,
        sun: SunPosition,
        ambient_temp_c: float,
        hour: Optional[int] = None,
        cloud_cover: Optional[float] = None,
        visibility_m: Optional[float] = None,
        precipitation_mm: Optional[float] = None,
        temperature_c: Optional[float] = None,
        snow_covered_panels: bool = False,
        frost_detected: Optional[str] = None,
        frost_score: Optional[int] = None,
        snow_per_group: Optional[Dict[str, bool]] = None,
        ghi_ratio: Optional[float] = None,
    ) -> MultiGroupResult:
        # fmt: off
        """Calculate total power output for all panel groups with calibration. @zara

Args:
    irradiance: Solar irradiance components (GHI, DNI, DHI)
    sun: Current sun position
    ambient_temp_c: Ambient temperature in Celsius
    hour: Optional hour for hourly-specific calibration factors
    cloud_cover: Optional cloud cover (0-100)
    visibility_m: Optional visibility in meters
    precipitation_mm: Optional precipitation in mm/h
    temperature_c: Optional temperature in Celsius
    snow_covered_panels: Whether panels are snow-covered (system-wide fallback)
    frost_detected: Optional frost detection status
    frost_score: Optional frost score 0-10
    snow_per_group: Optional per-group snow coverage Dict[group_name, bool]

Returns:
    MultiGroupResult with total power and per-group breakdown
"""
        # fmt: on
        if not self._groups:
            return MultiGroupResult(
                total_power_kwh=0.0, group_results=[], total_capacity_kwp=0.0
            )
        group_results = []
        total_power_kwh = 0.0
        for group in self._groups:
            group_snow_covered = snow_covered_panels
            if snow_per_group:
                group_snow_covered = snow_per_group.get(group.name, snow_covered_panels)
            result = self.calculate_group_power(
                group,
                irradiance,
                sun,
                ambient_temp_c,
                hour,
                cloud_cover,
                visibility_m,
                precipitation_mm,
                temperature_c,
                group_snow_covered,
                frost_detected,
                frost_score,
                ghi_ratio,
            )
            group_results.append(result)
            total_power_kwh += result.power_kwh
        if total_power_kwh > 0:
            for result in group_results:
                result.contribution_percent = result.power_kwh / total_power_kwh * 100
        return MultiGroupResult(
            total_power_kwh=total_power_kwh,
            group_results=group_results,
            total_capacity_kwp=self.total_capacity_kwp,
        )

    def calculate_hourly_forecast(
        self, weather_data: dict, astronomy_data: dict
    ) -> dict:
        # fmt: off
        """Calculate power forecast from weather and astronomy data for all groups. @zara

Args:
    weather_data: Dict with weather info (ghi, dni, dhi, temperature, etc.)
    astronomy_data: Dict with sun position (elevation_deg, azimuth_deg)

Returns:
    Dict with forecast results including per-group breakdown
"""
        # fmt: on
        sun_elevation = astronomy_data.get("elevation_deg", 0.0) or 0.0
        ghi, dni, dhi = poa_irradiance_components(
            weather_data.get("ghi", 0.0),
            weather_data.get("direct_radiation", weather_data.get("dni", 0.0)),
            weather_data.get("diffuse_radiation", weather_data.get("dhi", 0.0)),
            sun_elevation,
        )
        irradiance = IrradianceData(ghi=ghi, dni=dni, dhi=dhi)
        sun_azimuth = astronomy_data.get("azimuth_deg", 180.0) or 180.0
        sun = SunPosition(elevation_deg=sun_elevation, azimuth_deg=sun_azimuth)
        ambient_temperature = weather_data.get("temperature", 15.0) or 15.0
        result = self.calculate_total_power(irradiance, sun, ambient_temperature)
        return {
            "physics_prediction_kwh": round(result.total_power_kwh, 4),
            "total_capacity_kwp": round(result.total_capacity_kwp, 3),
            "group_count": len(result.group_results),
            "groups": result.to_dict()["groups"],
            "input": {
                "ghi_wm2": ghi,
                "dni_wm2": dni,
                "dhi_wm2": dhi,
                "sun_elevation_deg": sun_elevation,
                "sun_azimuth_deg": sun_azimuth,
                "ambient_temp_c": ambient_temperature,
            },
        }

    def get_optimal_hours_by_group(
        self,
        hourly_sun_positions: List[dict],
        irradiance_data: List[IrradianceData],
        ambient_temp_c: float = 15.0,
    ) -> Dict[str, int]:
        # fmt: off
        """Find the optimal production hour for each panel group. @zara

Args:
    hourly_sun_positions: List of sun positions by hour (0-23)
    irradiance_data: List of irradiance data by hour
    ambient_temp_c: Ambient temperature

Returns:
    Dict mapping group name to optimal hour (0-23)
"""
        # fmt: on
        optimal_hours = {}
        for group in self._groups:
            best_power_kwh = 0.0
            best_hour = 12
            for hour_index, (sun_data, irradiance) in enumerate(
                zip(hourly_sun_positions, irradiance_data)
            ):
                sun = SunPosition(
                    elevation_deg=sun_data.get("elevation_deg", 0),
                    azimuth_deg=sun_data.get("azimuth_deg", 180),
                )
                result = self.calculate_group_power(
                    group, irradiance, sun, ambient_temp_c
                )
                if result.power_kwh > best_power_kwh:
                    best_power_kwh = result.power_kwh
                    best_hour = hour_index
            optimal_hours[group.name] = best_hour
        return optimal_hours

    def to_dict(self) -> dict:
        """Convert calculator state to dictionary. @zara"""
        return {
            "total_capacity_kwp": round(self.total_capacity_kwp, 3),
            "group_count": self.group_count,
            "albedo": self.albedo,
            "system_efficiency": self.system_efficiency,
            "groups": [group.to_dict() for group in self._groups],
        }

    def __repr__(self) -> str:
        """String representation. @zara"""
        return f"PanelGroupCalculator(groups={self.group_count}, total={self.total_capacity_kwp:.2f}kWp)"
