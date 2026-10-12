# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
# fmt: off
"""
Astronomy Cache Module for Solar Forecast ML V16.2.0.
Calculates and caches solar position data using pvlib-style calculations.
All data is stored in SQLite database via DatabaseManager.
"""
# fmt: on
import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
from ..data.db_manager import DatabaseManager
from ..physics.poa_beam import (
    DNI_MAX_WM2,
    HORIZONTAL_DIFFUSE_FRACTION,
    HORIZONTAL_DIRECT_FRACTION,
    POA_DNI_IAM_FORMULA_VERSION,
    beam_iam_martin_ruiz,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class PanelGroupTheoreticalMax:
    """Theoretical max output for a single panel group. @zara"""

    if True:
        name: str
        power_kwp: float
        azimuth_deg: float
        tilt_deg: float
        theoretical_kwh: float
        poa_wm2: float
        aoi_deg: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "power_kwp": round(self.power_kwp, 3),
            "azimuth_deg": self.azimuth_deg,
            "tilt_deg": self.tilt_deg,
            "theoretical_kwh": round(self.theoretical_kwh, 4),
            "poa_wm2": round(self.poa_wm2, 2),
            "aoi_deg": round(self.aoi_deg, 1),
        }


class AstronomyCache:
    """Calculate and cache astronomy data for solar forecasting using DB storage. @zara"""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.latitude = None
        self.longitude = None
        self.elevation_m = None
        self.timezone = None
        self._panel_groups = []

    def set_panel_groups(self, panel_groups: List[Dict[str, Any]]) -> None:
        """Set panel groups for theoretical max calculations. @zara"""
        self._panel_groups = panel_groups or []
        if self._panel_groups:
            _LOGGER.debug(f"Panel groups configured: {len(self._panel_groups)} groups")
        # Preserve the verified shared-return edge.
        return None if True else None

    def initialize_location(
        self,
        latitude: float,
        longitude: float,
        timezone_str: str,
        elevation_m: float = 0,
    ):
        """Initialize location parameters and save to DB. @zara"""
        self.latitude = latitude
        self.longitude = longitude
        self.elevation_m = elevation_m
        self.timezone = ZoneInfo(timezone_str)
        _LOGGER.debug(
            f"Astronomy Cache initialized: lat={latitude}, lon={longitude}, tz={timezone_str}, elev={elevation_m}m"
        )

    def _calculate_sun_position(
        self, dt: datetime, latitude: float, longitude: float
    ) -> Tuple[float, float]:
        """Calculate sun elevation and azimuth for given time using astronomical formulas. @zara"""
        utc_time = dt.astimezone(ZoneInfo("UTC"))
        year, month, day = (utc_time.year, utc_time.month, utc_time.day)
        hour = utc_time.hour + utc_time.minute / 60.0 + utc_time.second / 3600.0
        if month <= 2:
            year -= 1
            month += 12
        century = math.floor(year / 100)
        calendar_correction = 2 - century + math.floor(century / 4)
        julian_day = (
            math.floor(365.25 * (year + 4716))
            + math.floor(30.6001 * (month + 1))
            + day
            + calendar_correction
            - 1524.5
            + hour / 24.0
        )
        julian_century = (julian_day - 2451545.0) / 36525.0
        mean_longitude_deg = (
            280.46646
            + 36000.76983 * julian_century
            + 0.0003032 * julian_century * julian_century
        ) % 360
        mean_anomaly_deg = (
            357.52911
            + 35999.05029 * julian_century
            - 0.0001537 * julian_century * julian_century
        ) % 360
        mean_anomaly_rad = math.radians(mean_anomaly_deg)
        equation_of_center_deg = (
            (
                1.914602
                - 0.004817 * julian_century
                - 1.4e-05 * julian_century * julian_century
            )
            * math.sin(mean_anomaly_rad)
            + (0.019993 - 0.000101 * julian_century) * math.sin(2 * mean_anomaly_rad)
            + 0.000289 * math.sin(3 * mean_anomaly_rad)
        )
        true_longitude_deg = (mean_longitude_deg + equation_of_center_deg) % 360
        obliquity_deg = 23.439291 - 0.0130042 * julian_century
        obliquity_rad = math.radians(obliquity_deg)
        true_longitude_rad = math.radians(true_longitude_deg)
        right_ascension_deg = math.degrees(
            math.atan2(
                math.cos(obliquity_rad) * math.sin(true_longitude_rad),
                math.cos(true_longitude_rad),
            )
        )
        declination_deg = math.degrees(
            math.asin(math.sin(obliquity_rad) * math.sin(true_longitude_rad))
        )
        declination_rad = math.radians(declination_deg)
        greenwich_sidereal_deg = (
            280.46061837 + 360.98564736629 * (julian_day - 2451545.0)
        ) % 360
        local_sidereal_deg = (greenwich_sidereal_deg + longitude) % 360
        hour_angle_deg = (local_sidereal_deg - right_ascension_deg) % 360
        if hour_angle_deg > 180:
            hour_angle_deg -= 360
        hour_angle_rad = math.radians(hour_angle_deg)
        latitude_rad = math.radians(latitude)
        sin_elevation = math.sin(latitude_rad) * math.sin(declination_rad) + math.cos(
            latitude_rad
        ) * math.cos(declination_rad) * math.cos(hour_angle_rad)
        elevation_deg = math.degrees(math.asin(max(-1, min(1, sin_elevation))))
        cos_azimuth = (
            math.sin(declination_rad) - math.sin(latitude_rad) * sin_elevation
        ) / (math.cos(latitude_rad) * math.cos(math.radians(elevation_deg)))
        cos_azimuth = max(-1, min(1, cos_azimuth))
        azimuth_deg = math.degrees(math.acos(cos_azimuth))
        if hour_angle_deg > 0:
            azimuth_deg = 360 - azimuth_deg
        return (elevation_deg, azimuth_deg)

    def _calculate_sunrise_sunset(
        self, target_date: date, latitude: float, longitude: float, timezone: ZoneInfo
    ) -> Tuple[Optional[datetime], Optional[datetime], Optional[datetime]]:
        """Calculate sunrise, sunset, and solar noon for a given date. @zara"""
        solar_noon = None
        maximum_elevation_deg = -90
        for minute in range(600, 840):
            sample_time = datetime.combine(target_date, datetime.min.time())
            sample_time = sample_time.replace(
                hour=minute // 60, minute=minute % 60, tzinfo=timezone
            )
            elevation_deg, unused_azimuth_deg = self._calculate_sun_position(
                sample_time, latitude, longitude
            )
            if elevation_deg > maximum_elevation_deg:
                maximum_elevation_deg = elevation_deg
                solar_noon = sample_time
        if solar_noon is None:
            return (None, None, None)
        sunrise = None
        for hour in range(0, solar_noon.hour + 1):
            for minute in range(0, 60, 5):
                sample_time = datetime.combine(target_date, datetime.min.time())
                sample_time = sample_time.replace(
                    hour=hour, minute=minute, tzinfo=timezone
                )
                elevation_deg, unused_azimuth_deg = self._calculate_sun_position(
                    sample_time, latitude, longitude
                )
                if not elevation_deg > -0.833:
                    continue
                sunrise = sample_time
                break
            if sunrise:
                break
        sunset = None
        for hour in range(solar_noon.hour, 24):
            for minute in range(0, 60, 5):
                sample_time = datetime.combine(target_date, datetime.min.time())
                sample_time = sample_time.replace(
                    hour=hour, minute=minute, tzinfo=timezone
                )
                elevation_deg, unused_azimuth_deg = self._calculate_sun_position(
                    sample_time, latitude, longitude
                )
                if not elevation_deg < -0.833:
                    continue
                sunset = sample_time
                break
            if sunset:
                break
        return (sunrise, sunset, solar_noon)

    def _calculate_clear_sky_solar_radiation(
        self, elevation_deg: float, day_of_year: int
    ) -> float:
        """Calculate clear sky solar radiation using simplified model. @zara"""
        if elevation_deg <= 0:
            return 0.0
        solar_constant_wm2 = 1367
        distance_correction = 1 + 0.033 * math.cos(2 * math.pi * day_of_year / 365)
        elevation_rad = math.radians(elevation_deg)
        air_mass = 1 / (
            math.sin(elevation_rad) + 0.50572 * (elevation_deg + 6.07995) ** (-1.6364)
        )
        transmittance = 0.7**air_mass**0.678
        radiation_wm2 = (
            solar_constant_wm2
            * distance_correction
            * math.sin(elevation_rad)
            * transmittance
        )
        return max(0, radiation_wm2)

    def _calculate_theoretical_pv_output(
        self,
        solar_radiation_wm2: float,
        system_capacity_kwp: float,
        efficiency: float = 0.95,
    ) -> float:
        """Calculate theoretical PV output for one hour. @zara"""
        reference_irradiance_wm2 = 1000.0
        energy_kwh = (
            system_capacity_kwp
            * (solar_radiation_wm2 / reference_irradiance_wm2)
            * efficiency
            * 1.0
        )
        return max(0, energy_kwh)

    def _calculate_theoretical_pv_per_group(
        self,
        clear_sky_radiation_wm2: float,
        sun_elevation_deg: float,
        sun_azimuth_deg: float,
        efficiency: float = 0.95,
    ) -> Tuple[float, List[PanelGroupTheoreticalMax]]:
        """Calculate theoretical PV output per panel group based on orientation. @zara"""
        if not self._panel_groups or sun_elevation_deg <= 0:
            return (0.0, [])
        group_results = []
        total_kwh = 0.0
        for group_index, group in enumerate(self._panel_groups):
            group_name = group.get("name", f"Gruppe {group_index + 1}")
            power_wp = float(group.get("power_wp", 0))
            capacity_kwp = power_wp / 1000.0
            panel_azimuth_deg = float(group.get("azimuth", 180))
            panel_tilt_deg = float(group.get("tilt", 30))
            if capacity_kwp <= 0:
                continue
            aoi_deg = self._calculate_aoi(
                sun_elevation_deg, sun_azimuth_deg, panel_tilt_deg, panel_azimuth_deg
            )
            poa_wm2 = self._calculate_poa_from_ghi(
                clear_sky_radiation_wm2,
                sun_elevation_deg,
                sun_azimuth_deg,
                panel_tilt_deg,
                panel_azimuth_deg,
                aoi_deg,
            )
            reference_irradiance_wm2 = 1000.0
            group_kwh = capacity_kwp * (poa_wm2 / reference_irradiance_wm2) * efficiency
            group_results.append(
                PanelGroupTheoreticalMax(
                    name=group_name,
                    power_kwp=capacity_kwp,
                    azimuth_deg=panel_azimuth_deg,
                    tilt_deg=panel_tilt_deg,
                    theoretical_kwh=group_kwh,
                    poa_wm2=poa_wm2,
                    aoi_deg=aoi_deg,
                )
            )
            total_kwh += group_kwh
        return (total_kwh, group_results)

    def _calculate_aoi(
        self,
        sun_elevation_deg: float,
        sun_azimuth_deg: float,
        panel_tilt_deg: float,
        panel_azimuth_deg: float,
    ) -> float:
        """Calculate Angle of Incidence between sun and panel. @zara"""
        zenith_deg = 90.0 - sun_elevation_deg
        zenith_rad = math.radians(zenith_deg)
        tilt_rad = math.radians(panel_tilt_deg)
        relative_azimuth_rad = math.radians(sun_azimuth_deg - panel_azimuth_deg)
        cos_aoi = math.cos(zenith_rad) * math.cos(tilt_rad) + math.sin(
            zenith_rad
        ) * math.sin(tilt_rad) * math.cos(relative_azimuth_rad)
        cos_aoi = max(-1.0, min(1.0, cos_aoi))
        aoi_deg = math.degrees(math.acos(cos_aoi))
        return aoi_deg

    def _calculate_poa_from_ghi(
        self,
        ghi_wm2: float,
        sun_elevation_deg: float,
        sun_azimuth_deg: float,
        panel_tilt_deg: float,
        panel_azimuth_deg: float,
        aoi_deg: float,
        albedo: float = 0.2,
    ) -> float:
        """Calculate Plane of Array irradiance from GHI for specific panel orientation. @zara"""
        if ghi_wm2 <= 0 or sun_elevation_deg <= 0:
            return 0.0
        elevation_rad = math.radians(sun_elevation_deg)
        dni_wm2 = (
            ghi_wm2 / max(0.01, math.sin(elevation_rad)) * HORIZONTAL_DIRECT_FRACTION
        )
        dhi_wm2 = ghi_wm2 * HORIZONTAL_DIFFUSE_FRACTION
        dni_wm2 = min(dni_wm2, DNI_MAX_WM2)
        dhi_wm2 = max(dhi_wm2, ghi_wm2 * 0.1)
        if aoi_deg < 90:
            beam_poa_wm2 = (
                dni_wm2
                * math.cos(math.radians(aoi_deg))
                * beam_iam_martin_ruiz(aoi_deg)
            )
        else:
            beam_poa_wm2 = 0.0
        tilt_rad = math.radians(panel_tilt_deg)
        diffuse_poa_wm2 = dhi_wm2 * (1 + math.cos(tilt_rad)) / 2
        ground_poa_wm2 = ghi_wm2 * albedo * (1 - math.cos(tilt_rad)) / 2
        total_poa_wm2 = beam_poa_wm2 + diffuse_poa_wm2 + ground_poa_wm2
        return max(0.0, total_poa_wm2)

    async def build_cache_for_date(
        self, target_date: date, system_capacity_kwp: float
    ) -> bool:
        """Build astronomy cache for a specific date and save to DB. @zara"""
        if not all([self.latitude, self.longitude, self.timezone]):
            _LOGGER.error("Astronomy Cache not initialized with location")
            return False

        def _build_sync():
            try:
                sunrise, sunset, solar_noon = self._calculate_sunrise_sunset(
                    target_date, self.latitude, self.longitude, self.timezone
                )
                if not sunrise or not sunset or (not solar_noon):
                    _LOGGER.warning(f"Could not calculate sun times for {target_date}")
                    return
                daylight_hours = (sunset - sunrise).total_seconds() / 3600.0
                day_of_year = target_date.timetuple().tm_yday
                results = []
                for hour in range(24):
                    sample_time = datetime.combine(target_date, datetime.min.time())
                    sample_time = sample_time.replace(
                        hour=hour, minute=30, tzinfo=self.timezone
                    )
                    elevation_deg, azimuth_deg = self._calculate_sun_position(
                        sample_time, self.latitude, self.longitude
                    )
                    clear_sky_wm2 = self._calculate_clear_sky_solar_radiation(
                        elevation_deg, day_of_year
                    )
                    group_results = None
                    if self._panel_groups:
                        group_total_kwh, group_results = (
                            self._calculate_theoretical_pv_per_group(
                                clear_sky_wm2, elevation_deg, azimuth_deg
                            )
                        )
                        theoretical_kwh = group_total_kwh
                    else:
                        theoretical_kwh = self._calculate_theoretical_pv_output(
                            clear_sky_wm2, system_capacity_kwp
                        )
                    results.append(
                        {
                            "hour": hour,
                            "elevation": elevation_deg,
                            "azimuth": azimuth_deg,
                            "clear_sky_radiation": clear_sky_wm2,
                            "theoretical_max": theoretical_kwh,
                            "sunrise": sunrise,
                            "sunset": sunset,
                            "solar_noon": solar_noon,
                            "daylight_hours": daylight_hours,
                            "group_results": group_results,
                        }
                    )
                return results
            except Exception as error:
                _LOGGER.error(
                    f"Error building cache for {target_date}: {error}", exc_info=True
                )
                return

        loop = asyncio.get_running_loop()
        hourly_results = await loop.run_in_executor(None, _build_sync)
        if not hourly_results:
            return False
        cache_rows = [
            (
                target_date,
                hour_data["hour"],
                hour_data["elevation"],
                hour_data["azimuth"],
                hour_data["clear_sky_radiation"],
                hour_data["theoretical_max"],
                hour_data["sunrise"],
                hour_data["sunset"],
                hour_data["solar_noon"],
                hour_data["daylight_hours"],
                POA_DNI_IAM_FORMULA_VERSION,
            )
            for hour_data in hourly_results
        ]
        await self.db.executemany(
            "INSERT INTO astronomy_cache\n               (cache_date, hour, sun_elevation_deg, sun_azimuth_deg,\n                clear_sky_radiation_wm2, theoretical_max_kwh,\n                sunrise, sunset, solar_noon, daylight_hours, version)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n               ON CONFLICT(cache_date, hour) DO UPDATE SET\n                   sun_elevation_deg = excluded.sun_elevation_deg,\n                   sun_azimuth_deg = excluded.sun_azimuth_deg,\n                   clear_sky_radiation_wm2 = excluded.clear_sky_radiation_wm2,\n                   theoretical_max_kwh = excluded.theoretical_max_kwh,\n                   sunrise = excluded.sunrise,\n                   sunset = excluded.sunset,\n                   solar_noon = excluded.solar_noon,\n                   daylight_hours = excluded.daylight_hours,\n                   version = excluded.version",
            cache_rows,
        )
        group_rows = []
        for hour_data in hourly_results:
            group_results = hour_data.get("group_results")
            if group_results:
                for group_result in group_results:
                    group_rows.append(
                        (
                            target_date,
                            hour_data["hour"],
                            group_result.name,
                            group_result.power_kwp,
                            group_result.azimuth_deg,
                            group_result.tilt_deg,
                            group_result.theoretical_kwh,
                            group_result.poa_wm2,
                            group_result.aoi_deg,
                        )
                    )
        if group_rows:
            await self.db.executemany(
                "INSERT INTO astronomy_cache_panel_groups\n                   (cache_date, hour, group_name, power_kwp, azimuth_deg, tilt_deg,\n                    theoretical_kwh, poa_wm2, aoi_deg)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(cache_date, hour, group_name) DO UPDATE SET\n                       power_kwp = excluded.power_kwp,\n                       azimuth_deg = excluded.azimuth_deg,\n                       tilt_deg = excluded.tilt_deg,\n                       theoretical_kwh = excluded.theoretical_kwh,\n                       poa_wm2 = excluded.poa_wm2,\n                       aoi_deg = excluded.aoi_deg",
                group_rows,
            )
        return True

    async def rebuild_cache(
        self,
        system_capacity_kwp: float,
        start_date: Optional[date] = None,
        days_back: int = 30,
        days_ahead: int = 7,
    ) -> Dict:
        """Rebuild entire astronomy cache in database. @zara"""
        if start_date is None:
            start_date = datetime.now(self.timezone).date()
        _LOGGER.info(
            f"Rebuilding astronomy cache: {days_back} days back, {days_ahead} days ahead from {start_date}"
        )
        await self.db.execute(
            "INSERT INTO astronomy_system_info\n               (id, latitude, longitude, elevation_m, timezone, installed_capacity_kwp)\n               VALUES (1, ?, ?, ?, ?, ?)\n               ON CONFLICT(id) DO UPDATE SET\n                   latitude = excluded.latitude,\n                   longitude = excluded.longitude,\n                   elevation_m = excluded.elevation_m,\n                   timezone = excluded.timezone,\n                   installed_capacity_kwp = excluded.installed_capacity_kwp,\n                   updated_at = CURRENT_TIMESTAMP",
            (
                self.latitude,
                self.longitude,
                self.elevation_m,
                str(self.timezone),
                system_capacity_kwp,
            ),
        )
        first_date = start_date - timedelta(days=days_back)
        last_date = start_date + timedelta(days=days_ahead)
        dates = []
        current_date = first_date
        while current_date <= last_date:
            dates.append(current_date)
            current_date += timedelta(days=1)
        _LOGGER.info(f"Processing {len(dates)} days in parallel...")
        results = await asyncio.gather(
            *[
                self.build_cache_for_date(cache_date, system_capacity_kwp)
                for cache_date in dates
            ],
            return_exceptions=True,
        )
        success_count = sum((1 for result in results if result is True))
        error_count = len(results) - success_count
        _LOGGER.info(f"Astronomy cache: {success_count}/{len(dates)} days processed")
        return {
            "total_days": len(dates),
            "success_count": success_count,
            "error_count": error_count,
        }

    async def get_day_data(self, target_date: date) -> Optional[Dict]:
        """Get astronomy data for a specific date from database. @zara"""
        rows = await self.db.fetchall(
            "SELECT hour, sun_elevation_deg, sun_azimuth_deg,\n                      clear_sky_radiation_wm2, theoretical_max_kwh,\n                      sunrise, sunset, solar_noon, daylight_hours\n               FROM astronomy_cache\n               WHERE cache_date = ?\n               ORDER BY hour",
            (target_date,),
        )
        if not rows:
            return
        hourly_data = {}
        for row in rows:
            hourly_data[str(row[0])] = {
                "elevation_deg": row[1],
                "azimuth_deg": row[2],
                "clear_sky_solar_radiation_wm2": row[3],
                "theoretical_max_pv_kwh": row[4],
            }
        first_row = rows[0]
        return {
            "sunrise_local": first_row[5],
            "sunset_local": first_row[6],
            "solar_noon_local": first_row[7],
            "daylight_hours": first_row[8],
            "hourly": hourly_data,
        }

    async def get_hourly_data(
        self, target_date: date, target_hour: int
    ) -> Optional[Dict]:
        """Get astronomy data for a specific hour from database. @zara"""
        row = await self.db.fetchone(
            "SELECT sun_elevation_deg, sun_azimuth_deg,\n                      clear_sky_radiation_wm2, theoretical_max_kwh\n               FROM astronomy_cache\n               WHERE cache_date = ? AND hour = ?",
            (target_date, target_hour),
        )
        if not row:
            return
        return {
            "elevation_deg": row[0],
            "azimuth_deg": row[1],
            "clear_sky_solar_radiation_wm2": row[2],
            "theoretical_max_pv_kwh": row[3],
        }

    async def get_production_window(
        self, target_date: date
    ) -> Optional[Tuple[datetime, datetime]]:
        """Get production window for a date from database. @zara"""
        row = await self.db.fetchone(
            "SELECT sunrise, sunset FROM astronomy_cache WHERE cache_date = ? LIMIT 1",
            (target_date,),
        )
        if not row:
            return
        try:
            sunrise = datetime.fromisoformat(row[0])
            sunset = datetime.fromisoformat(row[1])
            production_start = sunrise - timedelta(minutes=30)
            production_end = sunset + timedelta(minutes=30)
            return (production_start, production_end)
        except (ValueError, TypeError) as error:
            _LOGGER.error(f"Error parsing production window: {error}")
            return
