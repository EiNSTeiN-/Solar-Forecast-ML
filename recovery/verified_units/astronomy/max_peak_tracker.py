# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
Max Peak Tracker for Solar Forecast ML V16.2.0.
Tracks and updates maximum PV output records per hour using database storage.
"""
import logging
from datetime import date
from typing import Dict, Optional
from ..data.db_manager import DatabaseManager

_LOGGER = logging.getLogger(__name__)


class MaxPeakTracker:
    """Track and update maximum PV output records per hour in database. @zara"""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def check_and_update_peak(
        self,
        target_date: date,
        target_hour: int,
        actual_kwh: float,
        conditions: Optional[Dict] = None,
    ) -> bool:
        """Check if this is a new peak for the hour and update database if so. @zara"""
        try:
            hourly_peak_row = await self.db.fetchone(
                "SELECT kwh FROM astronomy_hourly_peaks WHERE hour = ?", (target_hour,)
            )
            hourly_peak_kwh = hourly_peak_row[0] if hourly_peak_row else 0.0
            if actual_kwh <= hourly_peak_kwh:
                return False
            conditions = conditions or {}
            await self.db.execute(
                "INSERT INTO astronomy_hourly_peaks\n                   (hour, kwh, date, sun_elevation_deg, cloud_cover_percent,\n                    temperature_c, solar_radiation_wm2)\n                   VALUES (?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(hour) DO UPDATE SET\n                       kwh = excluded.kwh,\n                       date = excluded.date,\n                       sun_elevation_deg = excluded.sun_elevation_deg,\n                       cloud_cover_percent = excluded.cloud_cover_percent,\n                       temperature_c = excluded.temperature_c,\n                       solar_radiation_wm2 = excluded.solar_radiation_wm2",
                (
                    target_hour,
                    actual_kwh,
                    target_date,
                    conditions.get("sun_elevation_deg"),
                    conditions.get("cloud_cover_percent"),
                    conditions.get("temperature_c"),
                    conditions.get("solar_radiation_wm2"),
                ),
            )
            global_peak_row = await self.db.fetchone(
                "SELECT max_peak_record_kwh FROM astronomy_system_info WHERE id = 1"
            )
            global_peak_kwh = global_peak_row[0] if global_peak_row else 0.0
            if actual_kwh > global_peak_kwh:
                await self.db.execute(
                    "UPDATE astronomy_system_info\n                       SET max_peak_record_kwh = ?,\n                           max_peak_date = ?,\n                           max_peak_hour = ?,\n                           max_peak_sun_elevation_deg = ?,\n                           max_peak_cloud_cover_percent = ?,\n                           max_peak_temperature_c = ?,\n                           max_peak_solar_radiation_wm2 = ?,\n                           updated_at = CURRENT_TIMESTAMP\n                       WHERE id = 1",
                    (
                        actual_kwh,
                        target_date,
                        target_hour,
                        conditions.get("sun_elevation_deg"),
                        conditions.get("cloud_cover_percent"),
                        conditions.get("temperature_c"),
                        conditions.get("solar_radiation_wm2"),
                    ),
                )
            _LOGGER.info(
                f"New peak record for hour {target_hour}: {actual_kwh:.3f} kWh (previous: {hourly_peak_kwh:.3f} kWh)"
            )
        except Exception as error:
            _LOGGER.error(f"Error checking/updating peak: {error}", exc_info=True)
            return False
        return True

    async def get_historical_max_for_hour(self, hour: int) -> Optional[float]:
        """Get historical maximum kWh for a specific hour from database. @zara"""
        try:
            row = await self.db.fetchone(
                "SELECT kwh FROM astronomy_hourly_peaks WHERE hour = ?", (hour,)
            )
            if row:
                return row[0]
        except Exception as error:
            _LOGGER.error(f"Error getting historical max: {error}")
            return
        return 0.0

    async def get_all_hourly_peaks(self) -> Dict[int, Dict]:
        """Get all hourly peak records from database. @zara"""
        try:
            rows = await self.db.fetchall(
                "SELECT hour, kwh, date, sun_elevation_deg, cloud_cover_percent,\n                          temperature_c, solar_radiation_wm2\n                   FROM astronomy_hourly_peaks\n                   ORDER BY hour"
            )
            peaks = {}
            for row in rows:
                peaks[row[0]] = {
                    "kwh": row[1],
                    "date": str(row[2]) if row[2] else None,
                    "conditions": {
                        "sun_elevation_deg": row[3],
                        "cloud_cover_percent": row[4],
                        "temperature_c": row[5],
                        "solar_radiation_wm2": row[6],
                    },
                }
            return peaks
        except Exception as error:
            _LOGGER.error(f"Error getting all hourly peaks: {error}")
            return {}

    async def get_global_max(self) -> Optional[Dict]:
        """Get global maximum peak record from database. @zara"""
        try:
            row = await self.db.fetchone(
                "SELECT max_peak_record_kwh, max_peak_date, max_peak_hour,\n                          max_peak_sun_elevation_deg, max_peak_cloud_cover_percent,\n                          max_peak_temperature_c, max_peak_solar_radiation_wm2\n                   FROM astronomy_system_info WHERE id = 1"
            )
            if not row or not row[0]:
                return None
            return {
                "kwh": row[0],
                "date": str(row[1]) if row[1] else None,
                "hour": row[2],
                "conditions": {
                    "sun_elevation_deg": row[3],
                    "cloud_cover_percent": row[4],
                    "temperature_c": row[5],
                    "solar_radiation_wm2": row[6],
                },
            }
        except Exception as error:
            _LOGGER.error(f"Error getting global max: {error}")
            return
