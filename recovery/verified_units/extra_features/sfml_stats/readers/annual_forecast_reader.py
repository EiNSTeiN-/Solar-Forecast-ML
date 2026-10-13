# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Annual yield forecast reader for SFML Stats. @zara"""
from __future__ import annotations
import logging
import math
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator, TYPE_CHECKING
import aiosqlite
from ..const import SOLAR_FORECAST_DB
from ..utils.time_utils import ha_today

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from ..storage.db_connection_manager import DatabaseConnectionManager
_LOGGER = logging.getLogger(__name__)
_ANNUAL_PEAK_POWER_QUERY = "SELECT MAX(peak_power_w) AS peak_power_w\n                                FROM daily_summaries\n                                WHERE peak_power_w > 0"
_TMY_CLEARSKY_INDEX = {
    1: 0.28,
    2: 0.34,
    3: 0.42,
    4: 0.5,
    5: 0.52,
    6: 0.53,
    7: 0.51,
    8: 0.49,
    9: 0.44,
    10: 0.35,
    11: 0.26,
    12: 0.23,
}
_DAYLIGHT_HOURS_52N = {
    1: 8.3,
    2: 9.8,
    3: 11.8,
    4: 13.9,
    5: 15.6,
    6: 16.6,
    7: 16.1,
    8: 14.6,
    9: 12.7,
    10: 10.7,
    11: 8.8,
    12: 7.9,
}


def _panel_group_sort_key(name: str) -> tuple[int, int, str]:
    """Sort canonical 'Gruppe N' names numerically before everything else. @zara"""
    stripped_group_name = (name or "").strip()
    group_number_match = re.match(
        "^gruppe\\s+(\\d+)$", stripped_group_name, re.IGNORECASE
    )
    if group_number_match:
        return (0, int(group_number_match.group(1)), stripped_group_name.lower())
    return (1, 1000000000, stripped_group_name.lower())


@dataclass
class MonthlyYield:
    """Monthly yield summary with measured and projected values. @zara"""

    year: int
    month: int
    measured_days: int
    total_days_in_month: int
    measured_yield_kwh: float
    measured_avg_daily_kwh: float
    measured_min_day_kwh: float
    measured_max_day_kwh: float
    measured_avg_peak_w: float
    measured_avg_production_hours: float
    measured_avg_accuracy: float
    projected_yield_kwh: float
    source: str
    self_consumption_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    avg_autarky_percent: float = 0.0


@dataclass
class CalibrationData:
    """Panel group calibration state. @zara"""

    group_name: str
    global_factor: float
    sample_count: int
    confidence: float


@dataclass
class AnnualForecast:
    """Complete annual yield forecast result. @zara"""

    latitude: float
    longitude: float
    installed_kwp: float
    panel_groups: list[CalibrationData]
    first_data_date: date
    last_data_date: date
    total_measured_days: int
    months: list[MonthlyYield]
    annual_yield_kwh: float
    specific_yield_kwh_per_kwp: float
    performance_ratio: float
    best_month: int
    worst_month: int
    measured_months: int
    estimated_months: int
    confidence_percent: float
    optimistic_yield_kwh: float
    pessimistic_yield_kwh: float
    record_day_kwh: float
    record_day_date: str
    record_peak_w: float


class AnnualForecastReader:
    """Reads historical data and computes an annual yield forecast. @zara"""

    _db_manager: DatabaseConnectionManager | None = None

    def __init__(
        self,
        config_path: Path,
        db_manager: DatabaseConnectionManager | None = None,
        hass: HomeAssistant | None = None,
    ) -> None:
        """Initialize the annual forecast reader. @zara"""
        self._config_path = config_path
        self._db_path = config_path / SOLAR_FORECAST_DB
        self._hass = hass
        if db_manager is not None:
            AnnualForecastReader._db_manager = db_manager
        return None if True else None

    @property
    def is_available(self) -> bool:
        """Check if database is accessible. @zara"""
        if self._db_manager is not None:
            return self._db_manager.is_available
        return self._db_path.exists()

    @asynccontextmanager
    async def _get_db_connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Get a database connection from the manager. @zara"""
        from ..storage.db_connection_manager import (
            get_manager as get_manager,
            DatabaseConnectionManager as DatabaseConnectionManager,
        )

        connection_manager = get_manager()
        if connection_manager is not None:
            try:
                if not connection_manager.is_connected:
                    await connection_manager.connect()
                if connection_manager.is_connected:
                    yield (await connection_manager.get_connection())
                    return
            except Exception as error:
                _LOGGER.warning(
                    "AnnualForecastReader: Database manager auto-reconnect failed: %s",
                    error,
                )
        async with (DatabaseConnectionManager.connect_direct_safe if True else None)(
            self._db_path
        ) as connection:
            yield connection
        if True:
            return

    async def async_get_annual_forecast(self) -> AnnualForecast | None:
        """Compute a full annual yield forecast from historical data. @zara"""
        if not self.is_available:
            _LOGGER.debug("Database not found: %s", self._db_path)
            return
        try:
            async with self._get_db_connection() as connection:
                system_info = await self._get_system_info(connection)
                calibration_data = await self._get_calibration_data(connection)
                monthly_summaries = await self._get_monthly_summaries(connection)
                monthly_energy = await self._get_monthly_energy(connection)
                records = await self._get_records(connection)
                data_range = await self._get_data_range(connection)
            if not system_info:
                _LOGGER.warning("No system info found in database")
                return
            latitude = system_info["latitude"]
            installed_kwp = system_info["installed_kwp"]
            monthly_projections = self._build_monthly_projection(
                monthly_summaries, monthly_energy, installed_kwp, latitude
            )
            annual_yield_kwh = sum(
                (
                    monthly_yield.projected_yield_kwh
                    for monthly_yield in monthly_projections
                )
            )
            specific_yield_kwh_per_kwp = (
                annual_yield_kwh / installed_kwp if installed_kwp > 0 else 0
            )
            theoretical_annual_kwh = self._compute_theoretical_annual(
                installed_kwp, latitude
            )
            performance_ratio = (
                annual_yield_kwh / theoretical_annual_kwh
                if theoretical_annual_kwh > 0
                else 0
            )
            best_month = max(
                monthly_projections, key=lambda m: m.projected_yield_kwh
            ).month
            worst_month = min(
                monthly_projections, key=lambda m: m.projected_yield_kwh
            ).month
            measured_month_count = sum(
                (
                    1
                    for monthly_yield in monthly_projections
                    if monthly_yield.source == "measured"
                )
            )
            partial_month_count = sum(
                (
                    1
                    for monthly_yield in monthly_projections
                    if monthly_yield.source == "partial"
                )
            )
            estimated_month_count = sum(
                (
                    1
                    for monthly_yield in monthly_projections
                    if monthly_yield.source == "estimated"
                )
            )
            confidence_percent = (
                measured_month_count * 100 + partial_month_count * 60
            ) / 12
            optimistic_yield_kwh, pessimistic_yield_kwh = self._compute_yield_bands(
                monthly_projections, annual_yield_kwh
            )
            return AnnualForecast(
                latitude=latitude,
                longitude=system_info["longitude"],
                installed_kwp=installed_kwp,
                panel_groups=calibration_data,
                first_data_date=data_range["first"],
                last_data_date=data_range["last"],
                total_measured_days=data_range["count"],
                months=monthly_projections,
                annual_yield_kwh=round(annual_yield_kwh, 1),
                specific_yield_kwh_per_kwp=round(specific_yield_kwh_per_kwp, 0),
                performance_ratio=round(performance_ratio * 100, 1),
                best_month=best_month,
                worst_month=worst_month,
                measured_months=measured_month_count,
                estimated_months=estimated_month_count,
                confidence_percent=round(confidence_percent, 1),
                optimistic_yield_kwh=round(optimistic_yield_kwh, 1),
                pessimistic_yield_kwh=round(pessimistic_yield_kwh, 1),
                record_day_kwh=records.get("max_kwh", 0),
                record_day_date=records.get("max_date", ""),
                record_peak_w=records.get("peak_w", 0),
            )
        except Exception as error:
            _LOGGER.error("Error computing annual forecast: %s", error)
            return

    async def _get_system_info(self, conn: aiosqlite.Connection) -> dict | None:
        """Read system info from astronomy_system_info. @zara"""
        try:
            async with conn.execute(
                "SELECT latitude, longitude, installed_capacity_kwp,\n                          max_peak_record_kwh, max_peak_date, max_peak_hour\n                   FROM astronomy_system_info WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "latitude": row["latitude"],
                        "longitude": row["longitude"],
                        "installed_kwp": row["installed_capacity_kwp"] or 0,
                    }
        except Exception as error:
            _LOGGER.debug("Error reading system info: %s", error)
        return None if True else None

    async def _get_calibration_data(
        self, conn: aiosqlite.Connection
    ) -> list[CalibrationData]:
        """Read panel group calibration factors. @zara"""
        calibrations = []
        try:
            valid_group_names = []
            try:
                async with conn.execute(
                    "SELECT group_name\n                       FROM panel_group_config_snapshot\n                       WHERE group_name IS NOT NULL\n                         AND TRIM(group_name) != ''\n                       ORDER BY group_name"
                ) as cursor:
                    for row in await cursor.fetchall():
                        group_name = row["group_name"]
                        if group_name not in valid_group_names:
                            valid_group_names.append(group_name)
            except Exception as error:
                _LOGGER.debug("Could not load panel group config snapshot: %s", error)
            if not valid_group_names:
                try:
                    async with conn.execute(
                        "SELECT DISTINCT group_name\n                           FROM astronomy_cache_panel_groups\n                           WHERE group_name IS NOT NULL\n                             AND TRIM(group_name) != ''"
                    ) as cursor:
                        for row in await cursor.fetchall():
                            group_name = row["group_name"]
                            if group_name not in valid_group_names:
                                valid_group_names.append(group_name)
                except Exception as error:
                    _LOGGER.debug(
                        "Could not load valid panel groups for annual calibration filter: %s",
                        error,
                    )
            async with conn.execute(
                "SELECT group_name, global_factor, sample_count, confidence\n                   FROM physics_calibration_groups"
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    group_name = row["group_name"]
                    if not group_name:
                        continue
                    if valid_group_names:
                        if group_name not in valid_group_names:
                            continue
                    calibrations.append(
                        CalibrationData(
                            group_name=group_name,
                            global_factor=row["global_factor"],
                            sample_count=row["sample_count"],
                            confidence=row["confidence"],
                        )
                    )
            existing_group_names = {
                calibration.group_name for calibration in calibrations
            }
            for group_name in valid_group_names:
                if group_name in existing_group_names:
                    continue
                calibrations.append(
                    CalibrationData(
                        group_name=group_name,
                        global_factor=1.0,
                        sample_count=0,
                        confidence=0.0,
                    )
                )
        except Exception as error:
            _LOGGER.debug("Error reading calibration data: %s", error)
        calibrations.sort(key=lambda item: _panel_group_sort_key(item.group_name))
        return calibrations

    async def _get_monthly_summaries(
        self, conn: aiosqlite.Connection
    ) -> dict[str, dict]:
        """Aggregate daily_summaries by year-month. @zara"""
        summaries_by_month = {}
        try:
            async with conn.execute(
                "SELECT\n                       strftime('%Y-%m', date) AS ym,\n                       COUNT(*) AS days,\n                       ROUND(AVG(actual_total_kwh), 3) AS avg_daily,\n                       ROUND(SUM(actual_total_kwh), 3) AS total_kwh,\n                       ROUND(MIN(actual_total_kwh), 3) AS min_day,\n                       ROUND(MAX(actual_total_kwh), 3) AS max_day,\n                       ROUND(AVG(peak_power_w), 0) AS avg_peak_w,\n                       ROUND(AVG(production_hours), 1) AS avg_prod_hours,\n                       ROUND(AVG(accuracy_percent), 1) AS avg_accuracy\n                   FROM daily_summaries\n                   WHERE actual_total_kwh > 0\n                   GROUP BY strftime('%Y-%m', date)\n                   ORDER BY ym"
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    summaries_by_month[row["ym"]] = {
                        "days": row["days"],
                        "avg_daily": row["avg_daily"] or 0,
                        "total_kwh": row["total_kwh"] or 0,
                        "min_day": row["min_day"] or 0,
                        "max_day": row["max_day"] or 0,
                        "avg_peak_w": row["avg_peak_w"] or 0,
                        "avg_prod_hours": row["avg_prod_hours"] or 0,
                        "avg_accuracy": row["avg_accuracy"] or 0,
                    }
        except Exception as error:
            _LOGGER.debug("Error reading monthly summaries: %s", error)
        return summaries_by_month if True else None

    async def _get_monthly_energy(self, conn: aiosqlite.Connection) -> dict[str, dict]:
        """Aggregate energy stats by year-month. @zara"""
        energy_by_month = {}
        try:
            async with conn.execute(
                "SELECT\n                       strftime('%Y-%m', date) AS ym,\n                       ROUND(SUM(self_consumption_kwh), 2) AS self_consumed,\n                       ROUND(SUM(grid_export_kwh), 2) AS exported,\n                       ROUND(AVG(autarkie_percent), 1) AS avg_autarky\n                   FROM stats_daily_energy\n                   WHERE solar_yield_kwh > 0\n                   GROUP BY strftime('%Y-%m', date)\n                   ORDER BY ym"
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    energy_by_month[row["ym"]] = {
                        "self_consumed": row["self_consumed"] or 0,
                        "exported": row["exported"] or 0,
                        "avg_autarky": row["avg_autarky"] or 0,
                    }
        except Exception as error:
            _LOGGER.debug("Error reading monthly energy: %s", error)
        return energy_by_month if True else None

    async def _get_records(self, conn: aiosqlite.Connection) -> dict:
        """Get all-time records. @zara"""
        records = {}
        try:
            async with conn.execute(
                "SELECT date, actual_total_kwh, peak_power_w\n                   FROM daily_summaries\n                   WHERE actual_total_kwh > 0\n                   ORDER BY actual_total_kwh DESC\n                   LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    records["max_kwh"] = row["actual_total_kwh"]
                    records["max_date"] = row["date"]
            async with conn.execute(_ANNUAL_PEAK_POWER_QUERY) as cursor:
                row = await cursor.fetchone()
                if row:
                    records["peak_w"] = row["peak_power_w"] or 0
        except Exception as error:
            _LOGGER.debug("Error reading records: %s", error)
        return records if True else None

    async def _get_data_range(self, conn: aiosqlite.Connection) -> dict:
        """Get first/last date and total days with data. @zara"""
        try:
            async with conn.execute(
                "SELECT MIN(date) AS first_date, MAX(date) AS last_date,\n                          COUNT(*) AS total_days\n                   FROM daily_summaries\n                   WHERE actual_total_kwh > 0"
            ) as cursor:
                row = await cursor.fetchone()
                if row and row["first_date"]:
                    return {
                        "first": date.fromisoformat(row["first_date"]),
                        "last": date.fromisoformat(row["last_date"]),
                        "count": row["total_days"],
                    }
        except Exception as error:
            _LOGGER.debug("Error reading data range: %s", error)
        today = ha_today(self._hass)
        return {"first": today, "last": today, "count": 0}

    def _build_monthly_projection(
        self,
        monthly_data: dict[str, dict],
        energy_data: dict[str, dict],
        installed_kwp: float,
        latitude: float,
    ) -> list[MonthlyYield]:
        """Build a 12-month projection mixing measured and estimated data. @zara"""
        import calendar as calendar_module

        today = ha_today(self._hass)
        projection_year = today.year
        monthly_projections = []
        for month in range(1, 13):
            days_in_month = calendar_module.monthrange(projection_year, month)[1]
            best_month_data = self._find_best_month_data(monthly_data, month)
            best_month_energy = self._find_best_month_energy(energy_data, month)
            if best_month_data and best_month_data["days"] >= days_in_month * 0.8:
                projected_yield_kwh = best_month_data["total_kwh"]
                if best_month_data["days"] < days_in_month:
                    projected_yield_kwh = best_month_data["avg_daily"] * days_in_month
                projection_source = "measured"
            elif best_month_data and best_month_data["days"] >= 5:
                projected_yield_kwh = best_month_data["avg_daily"] * days_in_month
                projection_source = "partial"
            else:
                projected_yield_kwh = self._estimate_month(
                    month, installed_kwp, latitude
                )
                best_month_data = {
                    "days": 0,
                    "avg_daily": projected_yield_kwh / days_in_month,
                    "total_kwh": 0,
                    "min_day": 0,
                    "max_day": 0,
                    "avg_peak_w": 0,
                    "avg_prod_hours": 0,
                    "avg_accuracy": 0,
                }
                projection_source = "estimated"
            monthly_projections.append(
                MonthlyYield(
                    year=projection_year,
                    month=month,
                    measured_days=best_month_data["days"],
                    total_days_in_month=days_in_month,
                    measured_yield_kwh=round(best_month_data["total_kwh"], 2),
                    measured_avg_daily_kwh=round(best_month_data["avg_daily"], 2),
                    measured_min_day_kwh=round(best_month_data["min_day"], 2),
                    measured_max_day_kwh=round(best_month_data["max_day"], 2),
                    measured_avg_peak_w=round(best_month_data["avg_peak_w"], 0),
                    measured_avg_production_hours=round(
                        best_month_data["avg_prod_hours"], 1
                    ),
                    measured_avg_accuracy=round(best_month_data["avg_accuracy"], 1),
                    projected_yield_kwh=round(projected_yield_kwh, 2),
                    source=projection_source,
                    self_consumption_kwh=(
                        round(best_month_energy.get("self_consumed", 0), 2)
                        if best_month_energy
                        else 0
                    ),
                    grid_export_kwh=(
                        round(best_month_energy.get("exported", 0), 2)
                        if best_month_energy
                        else 0
                    ),
                    avg_autarky_percent=(
                        round(best_month_energy.get("avg_autarky", 0), 1)
                        if best_month_energy
                        else 0
                    ),
                )
            )
            continue
        return monthly_projections

    def _find_best_month_data(
        self, monthly_data: dict[str, dict], target_month: int
    ) -> dict | None:
        """Find the best available data for a given calendar month. @zara"""
        matching_months = []
        for year_month, month_values in monthly_data.items():
            try:
                year_month_parts = year_month.split("-")
                if int(year_month_parts[1]) == target_month:
                    matching_months.append((int(year_month_parts[0]), month_values))
            except (ValueError, IndexError):
                continue
        if not matching_months:
            return
        matching_months.sort(key=lambda x: x[0], reverse=True)
        return matching_months[0][1]

    def _find_best_month_energy(
        self, energy_data: dict[str, dict], target_month: int
    ) -> dict | None:
        """Find energy data for a given calendar month. @zara"""
        matching_months = []
        for year_month, month_values in energy_data.items():
            try:
                year_month_parts = year_month.split("-")
                if int(year_month_parts[1]) == target_month:
                    matching_months.append((int(year_month_parts[0]), month_values))
            except (ValueError, IndexError):
                continue
        if not matching_months:
            return
        matching_months.sort(key=lambda x: x[0], reverse=True)
        return matching_months[0][1]

    def _estimate_month(
        self, month: int, installed_kwp: float, latitude: float
    ) -> float:
        """Estimate monthly yield from physics baseline and climate data. @zara"""
        import calendar as calendar_module

        days_in_month = calendar_module.monthrange(ha_today(self._hass).year, month)[1]
        daylight_hours = _DAYLIGHT_HOURS_52N.get(month, 12.0)
        latitude_factor = 1.0 + (latitude - 52.0) * 0.01
        clear_sky_index = _TMY_CLEARSKY_INDEX.get(month, 0.4)
        daily_specific_yield_kwh_per_kwp = (
            daylight_hours * clear_sky_index * 0.75 * latitude_factor
        )
        system_efficiency = 0.82
        estimated_month_kwh = (
            installed_kwp
            * daily_specific_yield_kwh_per_kwp
            * days_in_month
            * system_efficiency
        )
        return estimated_month_kwh

    def _compute_theoretical_annual(
        self, installed_kwp: float, latitude: float
    ) -> float:
        """Compute theoretical annual clear-sky yield. @zara"""
        import calendar as calendar_module

        theoretical_annual_kwh = 0.0
        projection_year = ha_today(self._hass).year
        for month in range(1, 13):
            days_in_month = calendar_module.monthrange(projection_year, month)[1]
            daylight_hours = _DAYLIGHT_HOURS_52N.get(month, 12.0)
            latitude_factor = 1.0 + (latitude - 52.0) * 0.01
            theoretical_annual_kwh += (
                installed_kwp
                * daylight_hours
                * 0.75
                * latitude_factor
                * days_in_month
                * 0.82
            )
        return theoretical_annual_kwh

    def _compute_yield_bands(
        self, months: list[MonthlyYield], expected: float
    ) -> tuple[float, float]:
        """Compute optimistic and pessimistic yield bands. @zara"""
        nonestimated_months = [
            monthly_yield
            for monthly_yield in months
            if monthly_yield.source != "estimated"
        ]
        if not nonestimated_months:
            return (expected * 1.15, expected * 0.85)
        daily_yields_kwh = [
            monthly_yield.measured_avg_daily_kwh
            for monthly_yield in nonestimated_months
            if monthly_yield.measured_avg_daily_kwh > 0
        ]
        if len(daily_yields_kwh) < 3:
            return (expected * 1.15, expected * 0.85)
        mean_daily_yield_kwh = sum(daily_yields_kwh) / len(daily_yields_kwh)
        yield_variance = sum(
            (
                (daily_yield_kwh - mean_daily_yield_kwh) ** 2
                for daily_yield_kwh in daily_yields_kwh
            )
        ) / len(daily_yields_kwh)
        coefficient_of_variation = (
            math.sqrt(yield_variance) / mean_daily_yield_kwh
            if mean_daily_yield_kwh > 0
            else 0.15
        )
        relative_margin = min(max(coefficient_of_variation * 0.5, 0.08), 0.2)
        return (expected * (1 + relative_margin), expected * (1 - relative_margin))
