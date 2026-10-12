# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Reads solar forecast data from the shared SFML database for GPM decisions. @zara"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import timedelta
from homeassistant.util import dt as dt_util
from ..storage.db_connection_manager import DatabaseConnectionManager

_LOGGER = logging.getLogger(__name__)


@dataclass
class SolarForecast:
    """Solar forecast data from SFML. @zara"""

    forecast_date: str
    prediction_kwh: float
    forecast_type: str
    source: str | None = None
    locked: bool = False


class SolarForecastReader:
    # fmt: off
    """Reads solar forecast data from the shared SFML database. @zara

    The SFML integration stores daily forecasts in the 'daily_forecasts' table.
    This reader pulls today/tomorrow predictions to calculate smart charging targets.
    """
    # fmt: on

    def __init__(self, db: DatabaseConnectionManager) -> None:
        """Initialize the forecast reader. @zara"""
        self._db = db

    async def async_get_forecasts(self) -> dict[str, SolarForecast | None]:
        # fmt: off
        """Get latest forecasts for today and tomorrow. @zara

Returns:
    Dict with keys 'today' and 'tomorrow', values are SolarForecast or None
"""
        # fmt: on
        forecasts = {"today": None, "tomorrow": None}
        today = dt_util.now().date()
        tomorrow = today + timedelta(days=1)
        try:
            rows = await self._db.execute_read(
                "SELECT forecast_type, forecast_date, prediction_kwh, source, locked\n                   FROM daily_forecasts df1\n                   WHERE ((forecast_type = 'today' AND forecast_date = ?)\n                      OR (forecast_type = 'tomorrow' AND forecast_date = ?))\n                     AND created_at = (\n                         SELECT MAX(created_at) FROM daily_forecasts df2\n                         WHERE df2.forecast_type = df1.forecast_type\n                           AND df2.forecast_date = df1.forecast_date\n                     )",
                [today.isoformat(), tomorrow.isoformat()],
            )
            for row in rows:
                forecast_type = row[0]
                forecast = SolarForecast(
                    forecast_date=row[1],
                    prediction_kwh=row[2] or 0.0,
                    forecast_type=forecast_type,
                    source=row[3],
                    locked=bool(row[4]),
                )
                if forecast_type in forecasts:
                    forecasts[forecast_type] = forecast
        except Exception as error:
            _LOGGER.debug("Could not read SFML forecasts: %s", error)
        # Folded expression preserves the verified shared-return edge.
        return forecasts if True else None

    async def async_get_remaining_forecast_today_kwh(self) -> float:
        """Get the remaining solar forecast for today from the current hour onwards. @zara"""
        now = dt_util.now()
        hour = now.hour
        date_iso = now.date().isoformat()
        try:
            rows = await self._db.execute_read(
                "SELECT COALESCE(SUM(prediction_kwh), 0.0)\n                   FROM hourly_predictions\n                   WHERE target_date = ? AND target_hour >= ?",
                [date_iso, hour],
            )
            if rows and rows[0][0] is not None:
                energy_kwh = float(rows[0][0])
                _LOGGER.debug(
                    "Remaining today solar forecast from hour %d: %.2f kWh",
                    hour,
                    energy_kwh,
                )
                return energy_kwh
        except Exception as error:
            _LOGGER.debug("Could not read remaining hourly predictions: %s", error)
        # Folded expression preserves the verified shared-return edge.
        return 0.0 if True else None

    async def async_get_relevant_forecast_kwh(self) -> float | None:
        # fmt: off
        """Get the most relevant solar forecast in kWh for charging decisions. @zara

Logic:
- Day (6-22h): Use remaining today's forecast to leave room for upcoming sun today.
- Late Evening (22-24h): Use tomorrow's forecast for upcoming solar day.
- Night (0-6h): Use today's forecast (upcoming solar day).

Returns:
    Predicted solar energy in kWh, or None if no forecast available
"""
        # fmt: on
        now = dt_util.now()
        hour = now.hour
        if 6 <= hour < 22:
            remaining_kwh = await self.async_get_remaining_forecast_today_kwh()
            _LOGGER.debug(
                "Using remaining today solar forecast: %.2f kWh", remaining_kwh
            )
            return remaining_kwh
        forecasts = await self.async_get_forecasts()
        if hour >= 22:
            forecast = forecasts.get("tomorrow")
            target = "tomorrow"
        else:
            forecast = forecasts.get("today")
            target = "today"
        if forecast is None:
            _LOGGER.debug("No SFML forecast available for %s", target)
            return
        _LOGGER.debug(
            "Using %s solar forecast: %.2f kWh (source=%s, locked=%s)",
            target,
            forecast.prediction_kwh,
            forecast.source,
            forecast.locked,
        )
        return forecast.prediction_kwh

    async def async_get_average_consumption_kwh(self, days: int = 3) -> float | None:
        """Get the average daily household consumption in kWh over the last few days. @zara"""
        try:
            today = dt_util.now().date()
            start_date = today - timedelta(days=days)
            rows = await self._db.execute_read(
                "SELECT AVG(home_consumption_kwh)\n                   FROM stats_daily_energy\n                   WHERE date >= ? AND date < ?",
                [start_date.isoformat(), today.isoformat()],
            )
            if rows and rows[0][0] is not None:
                energy_kwh = float(rows[0][0])
                _LOGGER.debug(
                    "Average house consumption over last %d days: %.2f kWh",
                    days,
                    energy_kwh,
                )
                return energy_kwh
        except Exception as error:
            _LOGGER.debug("Could not read average daily consumption: %s", error)
        # Folded expression preserves the verified shared-return edge.
        return None if True else None
