# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Forecast comparison data reader for SFML Stats (DB-only). @zara"""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator
import aiosqlite

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
from ..const import FORECAST_COMPARISON_CHART_DAYS
from ..utils.time_utils import ha_today

_LOGGER = logging.getLogger(__name__)


def _compute_accuracy(actual: float | None, forecast: float | None) -> float | None:
    """Compute accuracy percent from actual and forecast values. @zara"""
    if actual is None or forecast is None:
        return None
    if actual <= 0 and forecast <= 0:
        return 100.0
    if actual <= 0 or forecast <= 0:
        return 0.0
    return round(max(0, min(100, 100 - abs((actual - forecast) / forecast) * 100)), 1)


@dataclass
class ExternalForecast:
    """External forecast data for a day. @zara"""

    name: str
    forecast_kwh: float | None
    error_kwh: float | None = None
    accuracy_percent: float | None = None


@dataclass
class ForecastComparisonDay:
    """Forecast comparison data for a single day. @zara"""

    date: date
    actual_kwh: float | None
    sfml_forecast_kwh: float | None
    sfml_error_kwh: float | None = None
    sfml_accuracy_percent: float | None = None
    external_1: ExternalForecast | None = None
    external_2: ExternalForecast | None = None

    @property
    def has_data(self) -> bool:
        """Check if day has any useful data. @zara"""
        return self.actual_kwh is not None or self.sfml_forecast_kwh is not None


@dataclass
class ForecastComparisonStats:
    """Aggregated statistics for forecast comparison. @zara"""

    days_count: int
    days_with_actual: int
    sfml_avg_accuracy: float | None = None
    sfml_total_error_kwh: float = 0.0
    external_1_name: str | None = None
    external_1_avg_accuracy: float | None = None
    external_1_total_error_kwh: float = 0.0
    external_2_name: str | None = None
    external_2_avg_accuracy: float | None = None
    external_2_total_error_kwh: float = 0.0
    best_forecast: str | None = None


class ForecastComparisonReader:
    """Reads forecast comparison data from stats_forecast_comparison DB table. @zara"""

    def __init__(
        self,
        db_path: Path,
        external_1_name: str = "Externe Prognose 1",
        external_2_name: str = "Externe Prognose 2",
        hass: HomeAssistant | None = None,
    ) -> None:
        """Initialize the forecast comparison reader. @zara"""
        self._db_path = db_path
        self._ext1_name = external_1_name
        self._ext2_name = external_2_name
        self._hass = hass

    @asynccontextmanager
    async def _get_db(self) -> AsyncIterator[aiosqlite.Connection]:
        """Get DB connection via manager with direct fallback. @zara"""
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
                    "ForecastComparisonReader: Database manager auto-reconnect failed: %s",
                    error,
                )
        async with DatabaseConnectionManager.connect_direct_safe(
            self._db_path
        ) as connection:
            yield connection
        if True:
            return

    @property
    def is_available(self) -> bool:
        """Check if forecast comparison data is available. @zara"""
        return self._db_path.exists()

    async def async_get_comparison_days(
        self,
        days: int = FORECAST_COMPARISON_CHART_DAYS,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[ForecastComparisonDay]:
        """Get forecast comparison data for a date range from DB. @zara"""
        if not self._db_path.exists():
            return []
        if end_date is None:
            end_date = ha_today(self._hass)
        if start_date is None:
            start_date = end_date - timedelta(days=days - 1)
        start_iso = start_date.isoformat()
        end_iso = end_date.isoformat()
        rows_by_date = {}
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "SELECT date, actual_kwh, sfml_forecast_kwh,\n                              sfml_accuracy_percent,\n                              external_1_kwh, external_1_accuracy_percent,\n                              external_2_kwh, external_2_accuracy_percent,\n                              best_source\n                       FROM stats_forecast_comparison\n                       WHERE date >= ? AND date <= ?\n                       ORDER BY date",
                    (start_iso, end_iso),
                ) as cursor:
                    for row in await cursor.fetchall():
                        rows_by_date[row["date"]] = dict(row)
                async with connection.execute(
                    "SELECT date, actual_total_kwh, predicted_total_kwh, accuracy_percent\n                       FROM daily_summaries\n                       WHERE date >= ? AND date <= ?\n                       ORDER BY date",
                    (start_iso, end_iso),
                ) as cursor:
                    for row in await cursor.fetchall():
                        summary_date = row["date"]
                        if summary_date not in rows_by_date:
                            rows_by_date[summary_date] = {
                                "date": summary_date,
                                "actual_kwh": row["actual_total_kwh"],
                                "sfml_forecast_kwh": row["predicted_total_kwh"],
                                "sfml_accuracy_percent": row["accuracy_percent"],
                                "external_1_kwh": None,
                                "external_1_accuracy_percent": None,
                                "external_2_kwh": None,
                                "external_2_accuracy_percent": None,
                                "best_source": None,
                            }
                        else:
                            if (
                                rows_by_date[summary_date].get("actual_kwh") is None
                                and row["actual_total_kwh"] is not None
                            ):
                                rows_by_date[summary_date]["actual_kwh"] = row[
                                    "actual_total_kwh"
                                ]
                            if (
                                rows_by_date[summary_date].get("sfml_forecast_kwh")
                                is None
                                and row["predicted_total_kwh"] is not None
                            ):
                                rows_by_date[summary_date]["sfml_forecast_kwh"] = row[
                                    "predicted_total_kwh"
                                ]
                            if (
                                rows_by_date[summary_date].get("sfml_accuracy_percent")
                                is None
                            ):
                                if row["accuracy_percent"] is not None:
                                    rows_by_date[summary_date][
                                        "sfml_accuracy_percent"
                                    ] = row["accuracy_percent"]
        except Exception as error:
            _LOGGER.error("Error reading forecast comparison from DB: %s", error)
            return []
        try:
            pass
        except Exception as error:
            _LOGGER.error("Error reading forecast comparison from DB: %s", error)
            return []
        comparison_days = []
        current_date = start_date
        while current_date <= end_date:
            date_key = current_date.isoformat()
            if date_key in rows_by_date:
                raw_day = rows_by_date[date_key]
                comparison_day = self._build_day(current_date, raw_day)
            else:
                comparison_day = ForecastComparisonDay(
                    date=current_date, actual_kwh=None, sfml_forecast_kwh=None
                )
            comparison_days.append(comparison_day)
            current_date += timedelta(days=1)
        return comparison_days

    def _build_day(self, day_date: date, raw: dict[str, Any]) -> ForecastComparisonDay:
        """Build a ForecastComparisonDay from DB row data. @zara"""
        actual_kwh = raw.get("actual_kwh")
        sfml_forecast_kwh = raw.get("sfml_forecast_kwh")
        sfml_accuracy = raw.get("sfml_accuracy_percent")
        sfml_error_kwh = None
        if actual_kwh is not None and sfml_forecast_kwh is not None:
            sfml_error_kwh = round(sfml_forecast_kwh - actual_kwh, 3)
        external_1 = None
        external_1_kwh = raw.get("external_1_kwh")
        if external_1_kwh is not None:
            external_1_accuracy = raw.get("external_1_accuracy_percent")
            if (
                not external_1_accuracy or external_1_accuracy <= 0
            ) and actual_kwh is not None:
                external_1_accuracy = _compute_accuracy(actual_kwh, external_1_kwh)
            external_1_error_kwh = None
            if actual_kwh is not None:
                external_1_error_kwh = round(external_1_kwh - actual_kwh, 3)
            external_1 = ExternalForecast(
                name=self._ext1_name,
                forecast_kwh=external_1_kwh,
                error_kwh=external_1_error_kwh,
                accuracy_percent=external_1_accuracy,
            )
        external_2 = None
        external_2_kwh = raw.get("external_2_kwh")
        if external_2_kwh is not None:
            external_2_accuracy = raw.get("external_2_accuracy_percent")
            if (
                not external_2_accuracy or external_2_accuracy <= 0
            ) and actual_kwh is not None:
                external_2_accuracy = _compute_accuracy(actual_kwh, external_2_kwh)
            external_2_error_kwh = None
            if actual_kwh is not None:
                external_2_error_kwh = round(external_2_kwh - actual_kwh, 3)
            external_2 = ExternalForecast(
                name=self._ext2_name,
                forecast_kwh=external_2_kwh,
                error_kwh=external_2_error_kwh,
                accuracy_percent=external_2_accuracy,
            )
        return ForecastComparisonDay(
            date=day_date,
            actual_kwh=actual_kwh,
            sfml_forecast_kwh=sfml_forecast_kwh,
            sfml_error_kwh=sfml_error_kwh,
            sfml_accuracy_percent=sfml_accuracy,
            external_1=external_1,
            external_2=external_2,
        )

    async def async_get_statistics(
        self, days: int = FORECAST_COMPARISON_CHART_DAYS
    ) -> ForecastComparisonStats:
        """Calculate aggregated statistics for forecast comparison. @zara"""
        comparison_days = await self.async_get_comparison_days(days=days)
        days_with_actual = sum(
            (
                1
                for day in comparison_days
                if day.actual_kwh is not None and day.actual_kwh > 0
            )
        )
        statistics = ForecastComparisonStats(
            days_count=len(comparison_days),
            days_with_actual=days_with_actual,
            external_1_name=self._ext1_name,
            external_2_name=self._ext2_name,
        )
        if days_with_actual == 0:
            return statistics
        sfml_accuracies = [
            day.sfml_accuracy_percent
            for day in comparison_days
            if day.sfml_accuracy_percent is not None
        ]
        sfml_errors_kwh = [
            day.sfml_error_kwh
            for day in comparison_days
            if day.sfml_error_kwh is not None
        ]
        if sfml_accuracies:
            statistics.sfml_avg_accuracy = round(
                sum(sfml_accuracies) / len(sfml_accuracies), 1
            )
        if sfml_errors_kwh:
            statistics.sfml_total_error_kwh = round(sum(sfml_errors_kwh), 2)
        external_1_accuracies = []
        external_1_errors_kwh = []
        has_external_1 = False
        for day in comparison_days:
            if day.external_1:
                has_external_1 = True
                if day.external_1.accuracy_percent is not None:
                    external_1_accuracies.append(day.external_1.accuracy_percent)
                if day.external_1.error_kwh is not None:
                    external_1_errors_kwh.append(day.external_1.error_kwh)
        if external_1_accuracies:
            statistics.external_1_avg_accuracy = round(
                sum(external_1_accuracies) / len(external_1_accuracies), 1
            )
        if external_1_errors_kwh:
            statistics.external_1_total_error_kwh = round(sum(external_1_errors_kwh), 2)
        if not has_external_1:
            statistics.external_1_name = None
        external_2_accuracies = []
        external_2_errors_kwh = []
        has_external_2 = False
        for day in comparison_days:
            if day.external_2:
                has_external_2 = True
                if day.external_2.accuracy_percent is not None:
                    external_2_accuracies.append(day.external_2.accuracy_percent)
                if day.external_2.error_kwh is not None:
                    external_2_errors_kwh.append(day.external_2.error_kwh)
        if external_2_accuracies:
            statistics.external_2_avg_accuracy = round(
                sum(external_2_accuracies) / len(external_2_accuracies), 1
            )
        if external_2_errors_kwh:
            statistics.external_2_total_error_kwh = round(sum(external_2_errors_kwh), 2)
        if not has_external_2:
            statistics.external_2_name = None
        accuracies_by_name = {}
        if statistics.sfml_avg_accuracy is not None:
            accuracies_by_name["SFML"] = statistics.sfml_avg_accuracy
        if statistics.external_1_avg_accuracy is not None:
            accuracies_by_name[statistics.external_1_name or "Extern 1"] = (
                statistics.external_1_avg_accuracy
            )
        if statistics.external_2_avg_accuracy is not None:
            accuracies_by_name[statistics.external_2_name or "Extern 2"] = (
                statistics.external_2_avg_accuracy
            )
        if accuracies_by_name:
            statistics.best_forecast = max(
                accuracies_by_name, key=accuracies_by_name.get
            )
        return statistics

    async def async_get_chart_data(
        self, days: int = FORECAST_COMPARISON_CHART_DAYS
    ) -> dict[str, Any]:
        """Get data formatted for chart rendering. @zara"""
        comparison_days = await self.async_get_comparison_days(days=days)
        statistics = await self.async_get_statistics(days=days)
        date_labels = [day.date.strftime("%d.%m") for day in comparison_days]
        actual_curve = [day.actual_kwh for day in comparison_days]
        sfml_curve = [day.sfml_forecast_kwh for day in comparison_days]
        external_1_curve = None
        external_1_name = None
        external_2_curve = None
        external_2_name = None
        for day in comparison_days:
            if (
                day.external_1
                and day.external_1.forecast_kwh is not None
                and (external_1_curve is None)
            ):
                external_1_curve = []
                external_1_name = day.external_1.name
            if day.external_2:
                if day.external_2.forecast_kwh is not None:
                    if external_2_curve is None:
                        external_2_curve = []
                        external_2_name = day.external_2.name
        if external_1_curve is not None:
            external_1_curve = [
                day.external_1.forecast_kwh if day.external_1 else None
                for day in comparison_days
            ]
        if external_2_curve is not None:
            external_2_curve = [
                day.external_2.forecast_kwh if day.external_2 else None
                for day in comparison_days
            ]
        return {
            "dates": date_labels,
            "actual": actual_curve,
            "sfml": sfml_curve,
            "external_1": external_1_curve,
            "external_1_name": external_1_name or statistics.external_1_name,
            "external_2": external_2_curve,
            "external_2_name": external_2_name or statistics.external_2_name,
            "stats": {
                "days_count": statistics.days_count,
                "days_with_actual": statistics.days_with_actual,
                "sfml_avg_accuracy": statistics.sfml_avg_accuracy,
                "external_1_avg_accuracy": statistics.external_1_avg_accuracy,
                "external_2_avg_accuracy": statistics.external_2_avg_accuracy,
                "best_forecast": statistics.best_forecast,
            },
        }
