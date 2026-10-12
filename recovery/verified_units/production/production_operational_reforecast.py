# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
Operational intraday reforecast engine for the sidecar forecast track.

Rebuilds only future operational forecast hours and keeps the core
learning truth untouched.

@zara
"""
import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from .production_forecast_regeneration import ForecastRegenerationHelper
from .production_rest_of_day_uplift import cap_rest_of_day_uplift
from ..forecast.forecast_sun_gate import (
    should_zero_hourly_yield,
    zero_hourly_yield_entry,
)

if TYPE_CHECKING:
    from ..coordinator import SolarForecastMLCoordinator
    from ..data.db_manager import DatabaseManager
_LOGGER = logging.getLogger(__name__)


class OperationalReforecastEngine:
    """Future-only operational reforecast writer for the sidecar track. @zara"""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: "SolarForecastMLCoordinator",
        db_manager: "DatabaseManager",
    ):
        self.hass = hass
        self.coordinator = coordinator
        self.db = db_manager
        self._forecast_regeneration = ForecastRegenerationHelper(
            hass, coordinator, db_manager
        )
        self._runtime_lock = asyncio.Lock()

    async def run_reforecast(
        self,
        trigger_source: str = "manual",
        cutoff_hour: Optional[int] = None,
        now: Optional[datetime] = None,
        snapshot_type: str = "intraday_reforecast",
        rest_of_day_uplift: float = 1.0,
    ) -> bool:
        """Rebuild only future operational hours from the next full hour onward. @zara"""
        now = now or dt_util.now()
        today_date = now.date().isoformat()
        cutoff_hour = now.hour if cutoff_hour is None else cutoff_hour
        data_manager = getattr(self.coordinator, "data_manager", None)
        operational_handler = getattr(
            data_manager, "operational_forecast_handler", None
        )
        if operational_handler is None:
            _LOGGER.error(
                "Operational reforecast unavailable: no operational forecast handler"
            )
            return False
        if self._runtime_lock.locked():
            _LOGGER.info(
                "Operational reforecast skipped: another run is already active (trigger_source=%s, cutoff_hour=%s)",
                trigger_source,
                cutoff_hour,
            )
            return False
        async with self._runtime_lock:
            return await self._run_reforecast_locked(
                operational_handler=operational_handler,
                trigger_source=trigger_source,
                cutoff_hour=cutoff_hour,
                now=now,
                today=today_date,
                snapshot_type=snapshot_type,
                rest_of_day_uplift=rest_of_day_uplift,
            )
        return None if True else None

    async def _run_reforecast_locked(
        self,
        operational_handler: Any,
        trigger_source: str,
        cutoff_hour: int,
        now: datetime,
        today: str,
        snapshot_type: str,
        rest_of_day_uplift: float = 1.0,
    ) -> bool:
        try:
            _LOGGER.info(
                "Operational reforecast: starting for %s with cutoff_hour=%d",
                today,
                cutoff_hour,
            )
            await self._forecast_regeneration.refresh_weather_data(
                log_prefix="Operational reforecast"
            )
            hourly_values, weather_by_date_hour = (
                await self._forecast_regeneration.generate_full_forecast(
                    today, log_prefix="Operational reforecast"
                )
            )
            if not hourly_values:
                raise RuntimeError(
                    "Operational reforecast could not generate hourly forecast"
                )
            future_entries = self._build_future_entries(
                today=today, cutoff_hour=cutoff_hour, hourly_values=hourly_values
            )
            applied_uplift = self._apply_rest_of_day_uplift(
                future_entries, rest_of_day_uplift
            )
            weather_by_prediction_id = self._build_future_weather_map(
                today=today,
                cutoff_hour=cutoff_hour,
                weather_by_date_hour=weather_by_date_hour,
            )
            if not future_entries:
                _LOGGER.warning(
                    "Operational reforecast skipped: no future forecast rows available after cutoff_hour=%d",
                    cutoff_hour,
                )
                return False
            published = await operational_handler.publish_intraday_reforecast(
                snapshot_date=today,
                snapshot_type=snapshot_type,
                trigger_source=trigger_source,
                source_method="operational_intraday_reforecast",
                source_track="operational_reforecast",
                hourly_values=future_entries,
                daily_values=[
                    {
                        "forecast_type": "today",
                        "forecast_date": today,
                        "prediction_kwh": self._compute_rest_of_day_total(
                            future_entries
                        ),
                        "prediction_kwh_raw": self._compute_rest_of_day_total(
                            future_entries
                        ),
                        "actual_kwh": None,
                    }
                ],
                cutoff_hour=cutoff_hour,
                weather_by_prediction_id=weather_by_prediction_id,
            )
            snapshot_sequence = published.get("snapshot_sequence")
            if snapshot_sequence is None:
                raise RuntimeError(
                    "Operational intraday publish did not create a snapshot"
                )
            expected_weather_rows = sum(
                (
                    1
                    for entry in future_entries
                    if weather_by_prediction_id.get(
                        f"{entry['date']}_{entry['hour']:02d}"
                    )
                    is not None
                )
            )
            expected_group_rows = sum(
                (len(entry.get("groups") or []) for entry in future_entries)
            )
            if published["hourly_rows"] != len(future_entries):
                raise RuntimeError(
                    f"Operational reforecast write incomplete: {published['hourly_rows']} of {len(future_entries)} hourly rows persisted"
                )
            if published["weather_rows"] != expected_weather_rows:
                raise RuntimeError(
                    f"Operational reforecast weather write incomplete: {published['weather_rows']} of {expected_weather_rows} rows persisted"
                )
            if published["group_rows"] != expected_group_rows:
                raise RuntimeError(
                    f"Operational reforecast group write incomplete: {published['group_rows']} of {expected_group_rows} rows persisted"
                )
            if published["daily_rows"] != 1:
                raise RuntimeError("Operational daily forecast write incomplete")
            rest_of_day_total = self._compute_rest_of_day_total(future_entries)
            _LOGGER.info(
                "Operational reforecast complete: cutoff_hour=%d, future_rows=%d, new_total=%.2f kWh, rest_of_day_uplift=%.3f",
                cutoff_hour,
                published["hourly_rows"],
                rest_of_day_total,
                applied_uplift,
            )
            self._notify_operational_forecast_updated()
        except Exception as error:
            _LOGGER.error("Operational reforecast failed: %s", error, exc_info=True)
            return False
        return True

    def _apply_rest_of_day_uplift(
        self, future_entries: List[Dict[str, Any]], rest_of_day_uplift: float
    ) -> float:
        """Scale only future hours. Never reduces. Never touches past/morning anchors."""
        capped_uplift = cap_rest_of_day_uplift(rest_of_day_uplift)
        if capped_uplift <= 1.0:
            return 1.0
        forecast_fields = (
            "production_kwh",
            "prediction_kwh_uncapped",
            "physics_kwh",
            "ai_kwh",
            "lstm_kwh",
            "ridge_kwh",
            "tfs_kwh",
        )
        group_fields = (
            "power_kwh",
            "physics_kwh",
            "ai_kwh",
            "lstm_kwh",
            "ridge_kwh",
            "tfs_kwh",
            "raw_physics_kwh",
        )
        for entry in future_entries:
            self._scale_kwh_fields(entry, forecast_fields, capped_uplift)
            for group in entry.get("groups") or []:
                self._scale_kwh_fields(group, group_fields, capped_uplift)
            inverter_max_power = getattr(self.coordinator, "inverter_max_power", 0.0)
            if not inverter_max_power > 0:
                continue
            capped_production = min(
                float(entry.get("production_kwh") or 0.0), inverter_max_power
            )
            entry["production_kwh"] = round(capped_production, 4)
        return capped_uplift

    @staticmethod
    def _scale_kwh_fields(
        payload: Dict[str, Any], keys: tuple[str, ...], uplift: float
    ) -> None:
        for key in keys:
            value = payload.get(key)
            if not value is not None:
                continue
            try:
                payload[key] = round(float(value) * uplift, 4)
            except (TypeError, ValueError):
                continue

    def _notify_operational_forecast_updated(self) -> None:
        """Notify OPS-backed sensors after a successful sidecar rewrite."""
        try:
            notify_listeners = getattr(self.coordinator, "async_update_listeners", None)
            if callable(notify_listeners):
                notify_listeners()
        except Exception as error:
            _LOGGER.debug(
                "Operational reforecast listener notification failed: %s", error
            )
        return None if True else None

    def _build_future_entries(
        self, today: str, cutoff_hour: int, hourly_values: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Normalize only future reforecast rows for today. @zara"""
        inverter_max_power = getattr(self.coordinator, "inverter_max_power", 0.0)
        seen_prediction_ids = set()
        future_entries = []
        for row in hourly_values:
            if row.get("date") != today:
                continue
            hour = row.get("hour")
            if hour is None or hour <= cutoff_hour:
                continue
            prediction_id = f"{today}_{hour:02d}"
            if prediction_id in seen_prediction_ids:
                continue
            seen_prediction_ids.add(prediction_id)
            entry = dict(row)
            production_kwh = entry.get("production_kwh", 0.0)
            if inverter_max_power > 0:
                production_kwh = min(production_kwh, inverter_max_power)
            entry["production_kwh"] = production_kwh
            if should_zero_hourly_yield(
                sun_elevation_deg=entry.get("sun_elevation_deg")
            ) or entry.get("sun_gated"):
                zero_hourly_yield_entry(entry)
            future_entries.append(entry)
        return future_entries

    def _build_future_weather_map(
        self,
        today: str,
        cutoff_hour: int,
        weather_by_date_hour: Dict[tuple[str, int], Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """Build prediction-id keyed operational weather rows for future hours only. @zara"""
        weather_by_prediction_id = {}
        for (date_key, hour), weather_data in weather_by_date_hour.items():
            if date_key != today or hour is None or hour <= cutoff_hour:
                continue
            weather_by_prediction_id[f"{today}_{hour:02d}"] = weather_data
        return weather_by_prediction_id

    def _compute_rest_of_day_total(self, future_entries: List[Dict[str, Any]]) -> float:
        """Return the future-only operational total from the trigger onward. @zara"""
        return round(
            sum(
                (float(entry.get("production_kwh") or 0.0) for entry in future_entries)
            ),
            2,
        )
