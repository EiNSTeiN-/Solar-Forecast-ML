# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Modern-only weather and energy API. @zara"""

from __future__ import annotations
from datetime import date
from typing import TYPE_CHECKING
from aiohttp import web
from ..readers.weather_energy_reader import WeatherEnergyReader
from ..utils.time_utils import ha_today
from .modern import ModernAnalyticsView, _error
from .views import local_only

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class WeatherEnergyView(ModernAnalyticsView):
    """Shared date validation for historical weather-energy views. @zara"""

    reader: WeatherEnergyReader

    def _date(
        self, request: web.Request, *, required: bool = False
    ) -> str | None | web.Response:
        date_parameter = request.query.get("date")
        if not date_parameter:
            return (
                _error("missing_date", "The date parameter is required.", 400)
                if required
                else None
            )
        try:
            parsed_date = date.fromisoformat(date_parameter)
        except ValueError:
            return _error("invalid_date", "Expected date format YYYY-MM-DD.", 400)
        if parsed_date > ha_today(self.hass):
            return _error(
                "future_date",
                "Weather and energy analysis uses stored historical data.",
                400,
            )
        return parsed_date.isoformat()


class WeatherEnergySummaryView(WeatherEnergyView):
    url = "/api/sfml_stats/modern/weather-energy/summary"
    name = "api:sfml_stats:modern:weather-energy:summary"

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        target_date = self._date(request)
        if isinstance(target_date, web.Response):
            return target_date
        return await self._run(lambda: self.reader.get_summary(target_date))


class WeatherEnergyDayView(WeatherEnergyView):
    url = "/api/sfml_stats/modern/weather-energy/day"
    name = "api:sfml_stats:modern:weather-energy:day"

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        dev_error = self._require_dev()
        if dev_error is not None:
            return dev_error
        target_date = self._date(request)
        if isinstance(target_date, web.Response):
            return target_date
        return await self._run(lambda: self.reader.get_day(target_date))


class WeatherEnergyImpactView(WeatherEnergyView):
    url = "/api/sfml_stats/modern/weather-energy/impact"
    name = "api:sfml_stats:modern:weather-energy:impact"

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        dev_error = self._require_dev()
        if dev_error is not None:
            return dev_error
        days = self._days(request, default=90, allowed={30, 90, 180})
        if isinstance(days, web.Response):
            return days
        return await self._run(lambda: self.reader.get_impact(days))


class WeatherEnergyComparableDaysView(WeatherEnergyView):
    url = "/api/sfml_stats/modern/weather-energy/comparable-days"
    name = "api:sfml_stats:modern:weather-energy:comparable-days"

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        dev_error = self._require_dev()
        if dev_error is not None:
            return dev_error
        target_date = self._date(request)
        if isinstance(target_date, web.Response):
            return target_date
        return await self._run(lambda: self.reader.get_comparable_days(target_date))


async def async_setup_weather_energy_views(hass: HomeAssistant) -> None:
    """Register isolated weather-energy endpoints. @zara"""
    reader = WeatherEnergyReader(hass)
    for view_class in (
        WeatherEnergySummaryView,
        WeatherEnergyDayView,
        WeatherEnergyImpactView,
        WeatherEnergyComparableDaysView,
    ):
        hass.http.register_view(view_class(hass, reader))
