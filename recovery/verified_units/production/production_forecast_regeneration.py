# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
# fmt: off
"""
Shared forecast regeneration helper for same-day rebuild workflows.

Provides one consistent path for:
- weather refresh
- corrected weather regeneration
- full hourly forecast regeneration

@zara
"""
# fmt: on
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Tuple
from homeassistant.core import HomeAssistant
from ..forecast.forecast_strategy_base import ForecastGenerationError

if TYPE_CHECKING:
    from ..coordinator import SolarForecastMLCoordinator
    from ..data.db_manager import DatabaseManager
_LOGGER = logging.getLogger(__name__)


class ForecastRegenerationHelper:
    """Shared weather refresh and forecast regeneration helper. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: "SolarForecastMLCoordinator",
        db_manager: "DatabaseManager",
    ):
        self.hass = hass
        self.coordinator = coordinator
        self.db = db_manager

    async def refresh_weather_data(
        self, log_prefix: str = "Forecast regeneration"
    ) -> None:
        """Fetch fresh weather and rebuild corrected weather forecast. @zara"""
        try:
            pipeline = getattr(self.coordinator, "weather_pipeline_manager", None)
            if pipeline:
                _LOGGER.info("%s: Refreshing weather data from API...", log_prefix)
                refreshed = await pipeline.update_weather_cache(force=True)
                if refreshed:
                    _LOGGER.info("%s: Weather API refresh successful", log_prefix)
                else:
                    _LOGGER.warning(
                        "%s: Weather API refresh failed — using cached data", log_prefix
                    )
            else:
                _LOGGER.warning(
                    "%s: Weather pipeline not available — using cached data", log_prefix
                )
            corrector = (
                getattr(pipeline, "weather_corrector", None) if pipeline else None
            )
            if corrector:
                _LOGGER.info("%s: Rebuilding corrected forecast...", log_prefix)
                corrected = await corrector.create_corrected_forecast()
                if corrected:
                    _LOGGER.info("%s: Corrected forecast rebuilt", log_prefix)
                else:
                    _LOGGER.warning("%s: Corrected forecast rebuild failed", log_prefix)
            else:
                _LOGGER.warning("%s: Weather corrector not available", log_prefix)
        except Exception as error:
            _LOGGER.warning(
                "%s: Weather refresh error (non-fatal): %s", log_prefix, error
            )
        # Folded expression preserves the verified shared-return edge.
        return None if True else None

    async def generate_full_forecast(
        self, date: str, log_prefix: str = "Forecast regeneration"
    ) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, int], Dict[str, Any]]]:
        """Generate a full forecast using corrected weather input. @zara"""
        empty_result = ([], {})
        if (
            not hasattr(self.coordinator, "weather_service")
            or not self.coordinator.weather_service
        ):
            _LOGGER.error("%s: Weather service not available", log_prefix)
            return empty_result
        corrected_forecast = (
            await self.coordinator.weather_service.get_corrected_hourly_forecast(
                strict=True, allow_fallback=False
            )
        )
        if not corrected_forecast:
            _LOGGER.warning("%s: No corrected weather data available", log_prefix)
            return empty_result
        weather_by_hour = {}
        for weather_hour in corrected_forecast:
            hour_key = (weather_hour.get("date", date), weather_hour.get("hour"))
            weather_by_hour[hour_key] = weather_hour
        external_sensors = {}
        if hasattr(self.coordinator, "sensor_collector"):
            external_sensors = (
                self.coordinator.sensor_collector.collect_all_sensor_data_dict()
            )
        if not hasattr(self.coordinator, "forecast_orchestrator"):
            _LOGGER.error("%s: Forecast orchestrator not available", log_prefix)
            return empty_result
        try:
            forecast = (
                await self.coordinator.forecast_orchestrator.orchestrate_forecast(
                    current_weather=None,
                    hourly_forecast=corrected_forecast,
                    external_sensors=external_sensors,
                    correction_factor=getattr(
                        self.coordinator, "learned_correction_factor", 1.0
                    ),
                    persist=False,
                )
            )
        except ForecastGenerationError as error:
            _LOGGER.warning("%s: Forecast generation failed: %s", log_prefix, error)
            return empty_result
        hourly_forecast = forecast.get("hourly", [])
        if not hourly_forecast:
            _LOGGER.warning(
                "%s: Forecast orchestrator returned no hourly data", log_prefix
            )
            return empty_result
        _LOGGER.info(
            "%s: Full forecast generated — %d hours total",
            log_prefix,
            len(hourly_forecast),
        )
        return (hourly_forecast, weather_by_hour)
