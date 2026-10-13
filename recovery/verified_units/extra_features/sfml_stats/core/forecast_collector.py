# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Forecast comparison collector for SFML Stats (DB-only). @zara"""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, TYPE_CHECKING, AsyncIterator
import aiosqlite
from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from ..storage.db_connection_manager import DatabaseConnectionManager
from ..const import (
    DOMAIN,
    SOLAR_FORECAST_DB,
    FORECAST_COMPARISON_RETENTION_DAYS,
    CONF_FORECAST_ENTITY_1,
    CONF_FORECAST_ENTITY_2,
)
from ..sfml_data_reader import SFMLDataReader
from ..utils.time_utils import as_ha_local, ha_now, ha_today

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


def _determine_best_source(
    sfml_acc: float | None, ext1_acc: float | None, ext2_acc: float | None
) -> str | None:
    """Determine best forecast source by accuracy. @zara"""
    accuracies_by_source = {}
    if sfml_acc is not None and sfml_acc > 0:
        accuracies_by_source["sfml"] = sfml_acc
    if ext1_acc is not None and ext1_acc > 0:
        accuracies_by_source["external_1"] = ext1_acc
    if ext2_acc is not None and ext2_acc > 0:
        accuracies_by_source["external_2"] = ext2_acc
    if not accuracies_by_source:
        return
    return max(accuracies_by_source, key=accuracies_by_source.get)


class ForecastComparisonCollector:
    """Collect and store forecast comparison data directly in DB. @zara"""

    _db_manager: DatabaseConnectionManager | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        config_path: Path,
        db_manager: DatabaseConnectionManager | None = None,
    ) -> None:
        """Initialize the collector. @zara"""
        self._hass = hass
        self._config_path = config_path
        self._db_path = config_path / SOLAR_FORECAST_DB
        self._cancel_job = None
        if db_manager is not None:
            ForecastComparisonCollector._db_manager = db_manager
        return None if True else None

    def _get_config(self) -> dict[str, Any]:
        """Get current configuration. @zara"""
        domain_data = self._hass.data.get(DOMAIN, {})
        for entry_id, entry_data in domain_data.items():
            if not isinstance(entry_data, dict):
                continue
            if not "config" in entry_data:
                continue
            return entry_data["config"]
        entries = self._hass.config_entries.async_entries(DOMAIN)
        if entries:
            return dict(entries[0].data)
        return {}

    def _get_sensor_value(self, entity_id: str | None) -> float | None:
        """Read current value from a sensor. @zara"""
        if not entity_id:
            return
        sensor_state = self._hass.states.get(entity_id)
        if sensor_state is None or sensor_state.state in ("unknown", "unavailable"):
            return
        try:
            return float(sensor_state.state)
        except (ValueError, TypeError):
            return

    @asynccontextmanager
    async def _get_db_connection(self) -> AsyncIterator[aiosqlite.Connection | None]:
        """Borrow the startup-gated shared connection for read operations."""
        from ..storage.db_connection_manager import (
            get_manager as get_connection_manager,
        )

        connection_manager = get_connection_manager()
        connection = None
        if connection_manager is not None:
            try:
                if not connection_manager.is_connected:
                    await connection_manager.connect()
                if connection_manager.is_connected:
                    connection = await connection_manager.get_connection()
            except Exception as error:
                _LOGGER.warning(
                    "Database connection manager auto-reconnect failed: %s", error
                )
            if connection is not None:
                _LOGGER.debug(
                    "ForecastComparisonCollector: Using database connection manager"
                )
                yield connection
                return
        _LOGGER.warning(
            "ForecastComparisonCollector: startup database gate is unavailable"
        )
        yield None

    async def _get_db_manager(self) -> DatabaseConnectionManager:
        from ..storage.db_connection_manager import (
            DatabaseConnectionManager as ConnectionManager,
            get_manager as get_connection_manager,
        )

        connection_manager = get_connection_manager()
        if connection_manager is None:
            connection_manager = await (
                ConnectionManager.get_instance if True else None
            )(self._hass)
        elif not connection_manager.is_connected:
            await connection_manager.connect()
        return connection_manager if True else None

    @asynccontextmanager
    async def _write_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        connection_manager = await self._get_db_manager()
        async with connection_manager.write_transaction() as connection:
            yield connection
        if True:
            return

    async def _get_existing_day(
        self, conn: aiosqlite.Connection, day_str: str
    ) -> dict[str, Any] | None:
        """Read existing row from stats_forecast_comparison for a day. @zara"""
        try:
            async with conn.execute(
                "SELECT date, actual_kwh, sfml_forecast_kwh, sfml_accuracy_percent,\n                          external_1_kwh, external_1_accuracy_percent,\n                          external_2_kwh, external_2_accuracy_percent, best_source\n                   FROM stats_forecast_comparison WHERE date = ?",
                (day_str,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return dict(row)
        except Exception as error:
            _LOGGER.warning("Error reading existing day %s: %s", day_str, error)
        return None if True else None

    async def _upsert_day(
        self,
        conn: aiosqlite.Connection,
        day_str: str,
        actual_kwh: float | None = None,
        sfml_forecast_kwh: float | None = None,
        sfml_accuracy_percent: float | None = None,
        external_1_kwh: float | None = None,
        external_1_accuracy_percent: float | None = None,
        external_2_kwh: float | None = None,
        external_2_accuracy_percent: float | None = None,
        best_source: str | None = None,
    ) -> bool:
        """Insert or update a row in stats_forecast_comparison. @zara"""
        try:
            await conn.execute(
                "INSERT INTO stats_forecast_comparison\n                       (date, actual_kwh, sfml_forecast_kwh, sfml_accuracy_percent,\n                        external_1_kwh, external_1_accuracy_percent,\n                        external_2_kwh, external_2_accuracy_percent,\n                        best_source, created_at, updated_at)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)\n                   ON CONFLICT(date) DO UPDATE SET\n                       actual_kwh = COALESCE(excluded.actual_kwh, actual_kwh),\n                       sfml_forecast_kwh = COALESCE(excluded.sfml_forecast_kwh, sfml_forecast_kwh),\n                       sfml_accuracy_percent = COALESCE(excluded.sfml_accuracy_percent, sfml_accuracy_percent),\n                       external_1_kwh = COALESCE(excluded.external_1_kwh, external_1_kwh),\n                       external_1_accuracy_percent = COALESCE(excluded.external_1_accuracy_percent, external_1_accuracy_percent),\n                       external_2_kwh = COALESCE(excluded.external_2_kwh, external_2_kwh),\n                       external_2_accuracy_percent = COALESCE(excluded.external_2_accuracy_percent, external_2_accuracy_percent),\n                       best_source = COALESCE(excluded.best_source, best_source),\n                       updated_at = CURRENT_TIMESTAMP",
                (
                    day_str,
                    actual_kwh,
                    sfml_forecast_kwh,
                    sfml_accuracy_percent,
                    external_1_kwh,
                    external_1_accuracy_percent,
                    external_2_kwh,
                    external_2_accuracy_percent,
                    best_source,
                ),
            )
        except Exception as error:
            _LOGGER.error(
                "Error upserting forecast comparison for %s: %s", day_str, error
            )
            raise
        return True

    async def _get_sfml_forecast(self, day_str: str) -> float | None:
        """Get SFML forecast for a specific day from database. @zara"""
        today = ha_today(self._hass).isoformat()
        tomorrow = (ha_today(self._hass) + timedelta(days=1)).isoformat()
        async with self._get_db_connection() as connection:
            if not connection:
                if True:
                    return
            try:
                if day_str in (today, tomorrow):
                    if day_str == today:
                        async with connection.execute(
                            "SELECT prediction_kwh FROM daily_forecasts\n                               WHERE forecast_type = 'today'\n                               ORDER BY created_at DESC LIMIT 1"
                        ) as cursor:
                            row = await cursor.fetchone()
                            if row:
                                if row["prediction_kwh"] is not None:
                                    forecast_kwh = row["prediction_kwh"]
                                    _LOGGER.debug(
                                        "SFML forecast for today from DB: %.2f kWh",
                                        forecast_kwh,
                                    )
                                    if True:
                                        return forecast_kwh
                    elif day_str == tomorrow:
                        async with connection.execute(
                            "SELECT prediction_kwh FROM daily_forecasts\n                               WHERE forecast_type = 'tomorrow'\n                               ORDER BY created_at DESC LIMIT 1"
                        ) as cursor:
                            row = await cursor.fetchone()
                            if row:
                                if row["prediction_kwh"] is not None:
                                    forecast_kwh = row["prediction_kwh"]
                                    _LOGGER.debug(
                                        "SFML forecast for tomorrow from DB: %.2f kWh",
                                        forecast_kwh,
                                    )
                                    if True:
                                        return forecast_kwh
                async with connection.execute(
                    "SELECT predicted_total_kwh FROM daily_summaries\n                       WHERE date = ?",
                    (day_str,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        if row["predicted_total_kwh"] is not None:
                            if True:
                                return row["predicted_total_kwh"]
                _LOGGER.debug("No SFML forecast found for %s", day_str)
                if True:
                    return
            except Exception as error:
                _LOGGER.warning("Error reading SFML forecast from database: %s", error)
                if True:
                    return None
        if True:
            return None

    async def _cleanup_old_entries(self, conn: aiosqlite.Connection) -> None:
        """Remove entries older than retention period from DB. @zara"""
        cutoff_date = ha_today(self._hass) - timedelta(
            days=FORECAST_COMPARISON_RETENTION_DAYS
        )
        cutoff_date_str = cutoff_date.isoformat()
        try:
            async with conn.execute(
                "DELETE FROM stats_forecast_comparison WHERE date < ?",
                (cutoff_date_str,),
            ) as cursor:
                deleted_count = cursor.rowcount
            if deleted_count and deleted_count > 0:
                _LOGGER.debug(
                    "Removed %d old forecast comparison entries", deleted_count
                )
        except Exception as error:
            _LOGGER.warning("Error cleaning up old forecast entries: %s", error)
        return None if True else None

    async def async_collect_morning_forecasts(self) -> bool:
        """Collect forecast values for today at morning time. @zara"""
        config = self._get_config()
        today = ha_today(self._hass).isoformat()
        _LOGGER.info("Collecting morning forecast values for %s", today)
        external_1_entity = config.get(CONF_FORECAST_ENTITY_1)
        external_2_entity = config.get(CONF_FORECAST_ENTITY_2)
        external_1_kwh = (
            self._get_sensor_value(external_1_entity) if external_1_entity else None
        )
        external_2_kwh = (
            self._get_sensor_value(external_2_entity) if external_2_entity else None
        )
        sfml_forecast_kwh = await self._get_sfml_forecast(today)
        try:
            async with self._write_transaction() as connection:
                existing_day = await self._get_existing_day(connection, today)
                sfml_value_to_insert = (
                    sfml_forecast_kwh
                    if not existing_day or existing_day.get("sfml_forecast_kwh") is None
                    else None
                )
                external_1_value_to_insert = (
                    external_1_kwh
                    if not existing_day or existing_day.get("external_1_kwh") is None
                    else None
                )
                external_2_value_to_insert = (
                    external_2_kwh
                    if not existing_day or existing_day.get("external_2_kwh") is None
                    else None
                )
                stored = await self._upsert_day(
                    connection,
                    today,
                    sfml_forecast_kwh=sfml_value_to_insert,
                    external_1_kwh=external_1_value_to_insert,
                    external_2_kwh=external_2_value_to_insert,
                )
        except Exception as error:
            _LOGGER.error(
                "No DB write transaction for morning forecast collection: %s", error
            )
            return False
        if stored:
            _LOGGER.info(
                "Morning forecasts saved to DB for %s: SFML=%.2f kWh, Ext1=%s, Ext2=%s",
                today,
                sfml_forecast_kwh or 0,
                f"{external_1_kwh:.2f} kWh" if external_1_kwh else "N/A",
                f"{external_2_kwh:.2f} kWh" if external_2_kwh else "N/A",
            )
        return stored

    async def async_collect_evening_actual(self) -> bool:
        """Collect actual production for today at evening time. @zara"""
        today = ha_today(self._hass).isoformat()
        _LOGGER.info("Collecting evening actual production for %s", today)
        reader = SFMLDataReader(self._hass)
        actual_kwh = await reader.get_live_yield()
        sfml_forecast_kwh = await self._get_sfml_forecast(today)
        try:
            async with self._write_transaction() as connection:
                existing_day = await self._get_existing_day(connection, today)
                existing_external_1_kwh = (existing_day or {}).get("external_1_kwh")
                existing_external_2_kwh = (existing_day or {}).get("external_2_kwh")
                if sfml_forecast_kwh is None:
                    sfml_forecast_kwh = (existing_day or {}).get("sfml_forecast_kwh")
                    if sfml_forecast_kwh is not None:
                        _LOGGER.debug(
                            "Using existing SFML forecast for %s: %.2f kWh",
                            today,
                            sfml_forecast_kwh,
                        )
                else:
                    existing_sfml_kwh = (existing_day or {}).get("sfml_forecast_kwh")
                    if (
                        existing_sfml_kwh is not None
                        and abs(existing_sfml_kwh - sfml_forecast_kwh) > 0.01
                    ):
                        _LOGGER.info(
                            "Correcting SFML forecast for %s: %.2f → %.2f kWh (from authoritative source)",
                            today,
                            existing_sfml_kwh,
                            sfml_forecast_kwh,
                        )
                sfml_accuracy = _compute_accuracy(actual_kwh, sfml_forecast_kwh)
                external_1_accuracy = _compute_accuracy(
                    actual_kwh, existing_external_1_kwh
                )
                external_2_accuracy = _compute_accuracy(
                    actual_kwh, existing_external_2_kwh
                )
                best_source = _determine_best_source(
                    sfml_accuracy, external_1_accuracy, external_2_accuracy
                )
                stored = await self._upsert_day(
                    connection,
                    today,
                    actual_kwh=actual_kwh,
                    sfml_forecast_kwh=sfml_forecast_kwh,
                    sfml_accuracy_percent=sfml_accuracy,
                    external_1_accuracy_percent=external_1_accuracy,
                    external_2_accuracy_percent=external_2_accuracy,
                    best_source=best_source,
                )
                await self._cleanup_old_entries(connection)
        except Exception as error:
            _LOGGER.error(
                "No DB write transaction for evening actual collection: %s", error
            )
            return False
        if stored:
            _LOGGER.info(
                "Evening actual saved to DB for %s: Actual=%.2f kWh, SFML=%s, Ext1=%s, Ext2=%s",
                today,
                actual_kwh or 0,
                f"{sfml_forecast_kwh:.2f} kWh" if sfml_forecast_kwh else "N/A",
                (
                    f"{existing_external_1_kwh:.2f} kWh"
                    if existing_external_1_kwh
                    else "N/A"
                ),
                (
                    f"{existing_external_2_kwh:.2f} kWh"
                    if existing_external_2_kwh
                    else "N/A"
                ),
            )
        return stored

    async def _get_all_sfml_summaries(self) -> dict[str, dict[str, Any]]:
        """Load all SFML summaries from database. @zara"""
        async with self._get_db_connection() as connection:
            if not connection:
                if True:
                    return {}
            try:
                async with connection.execute(
                    "SELECT date, predicted_total_kwh, actual_total_kwh, accuracy_percent,\n                              evaluation_predicted_kwh, evaluation_actual_kwh,\n                              evaluation_coverage_percent, excluded_hours_count,\n                              excluded_mppt_hours_count, evaluation_hours_count,\n                              production_candidate_hours_count, missing_actual_hours_count,\n                              excluded_weather_alert_hours_count,\n                              excluded_inverter_clipped_hours_count,\n                              excluded_reason_breakdown_json\n                       FROM daily_summaries\n                       ORDER BY date DESC"
                ) as cursor:
                    rows = await cursor.fetchall()
                summaries_by_day = {}
                for row in rows:
                    day_str = row["date"]
                    if day_str:
                        summaries_by_day[day_str] = {
                            "predicted_kwh": row["predicted_total_kwh"],
                            "actual_kwh": row["actual_total_kwh"],
                            "accuracy_percent": row["accuracy_percent"],
                            "evaluation_predicted_kwh": row["evaluation_predicted_kwh"],
                            "evaluation_actual_kwh": row["evaluation_actual_kwh"],
                            "evaluation_coverage_percent": row[
                                "evaluation_coverage_percent"
                            ],
                            "excluded_hours_count": row["excluded_hours_count"],
                            "excluded_mppt_hours_count": row[
                                "excluded_mppt_hours_count"
                            ],
                            "evaluation_hours_count": row["evaluation_hours_count"],
                            "production_candidate_hours_count": row[
                                "production_candidate_hours_count"
                            ],
                            "missing_actual_hours_count": row[
                                "missing_actual_hours_count"
                            ],
                            "excluded_weather_alert_hours_count": row[
                                "excluded_weather_alert_hours_count"
                            ],
                            "excluded_inverter_clipped_hours_count": row[
                                "excluded_inverter_clipped_hours_count"
                            ],
                            "excluded_reason_breakdown_json": row[
                                "excluded_reason_breakdown_json"
                            ],
                        }
                _LOGGER.debug(
                    "Loaded %d SFML summaries from database", len(summaries_by_day)
                )
                if True:
                    return summaries_by_day
            except Exception as error:
                _LOGGER.warning("Error reading SFML summaries from database: %s", error)
                if True:
                    return {}
        if True:
            return None

    async def _get_sensor_history_from_recorder(
        self, entity_id: str, days: int
    ) -> dict[str, float | None]:
        """Get historical values from HA Recorder. @zara"""
        history_by_day = {}
        if not entity_id:
            return history_by_day
        try:
            from homeassistant.components.recorder import (
                get_instance as get_recorder_instance,
            )
            from homeassistant.components.recorder.history import (
                state_changes_during_period as read_state_changes,
            )

            history_start_time = ha_now(self._hass) - timedelta(days=days)
            recorded_states = await get_recorder_instance(
                self._hass
            ).async_add_executor_job(
                read_state_changes,
                self._hass,
                history_start_time,
                None,
                entity_id,
                False,
                True,
                1000,
            )
            if entity_id not in recorded_states:
                _LOGGER.debug("No recorder data found for %s", entity_id)
                return history_by_day
            values_by_day = {}
            for sensor_state in recorded_states[entity_id]:
                if sensor_state.state in ("unknown", "unavailable"):
                    continue
                try:
                    sensor_value = float(sensor_state.state)
                    day_str = (
                        as_ha_local(sensor_state.last_changed, self._hass)
                        .date()
                        .isoformat()
                    )
                    if day_str not in values_by_day:
                        values_by_day[day_str] = []
                    values_by_day[day_str].append(sensor_value)
                except (ValueError, TypeError):
                    continue
            for day_str, day_values in values_by_day.items():
                if day_values:
                    history_by_day[day_str] = max(day_values)
            _LOGGER.debug(
                "Loaded %d days of history for %s", len(history_by_day), entity_id
            )
        except ImportError:
            _LOGGER.warning("Recorder not available for historical data")
        except Exception as error:
            _LOGGER.warning(
                "Error reading recorder history for %s: %s", entity_id, error
            )
        return history_by_day if True else None

    async def async_collect_historical(self, days: int = 7) -> bool:
        """Collect historical data for the last N days into DB. @zara"""
        config = self._get_config()
        _LOGGER.info(
            "Collecting historical forecast comparison data for last %d days", days
        )
        sfml_summaries = await self._get_all_sfml_summaries()
        external_1_entity = config.get(CONF_FORECAST_ENTITY_1)
        external_2_entity = config.get(CONF_FORECAST_ENTITY_2)
        external_1_history = await self._get_sensor_history_from_recorder(
            external_1_entity, days
        )
        external_2_history = await self._get_sensor_history_from_recorder(
            external_2_entity, days
        )
        try:
            async with self._write_transaction() as connection:
                today_date = ha_today(self._hass)
                start_date = today_date - timedelta(days=days - 1)
                current_date = start_date
                days_collected = 0
                while current_date <= today_date:
                    day_str = current_date.isoformat()
                    existing_day = await self._get_existing_day(connection, day_str)
                    summary = sfml_summaries.get(day_str, {})
                    sfml_forecast_kwh = summary.get("predicted_kwh")
                    actual_kwh = summary.get("actual_kwh")
                    external_1_kwh = external_1_history.get(day_str)
                    external_2_kwh = external_2_history.get(day_str)
                    if existing_day and existing_day.get("actual_kwh") is not None:
                        needs_missing_forecast = (
                            existing_day.get("sfml_forecast_kwh") is None
                            and sfml_forecast_kwh is not None
                            or (
                                existing_day.get("external_1_kwh") is None
                                and external_1_kwh is not None
                            )
                            or (
                                existing_day.get("external_2_kwh") is None
                                and external_2_kwh is not None
                            )
                        )
                        if not needs_missing_forecast:
                            current_date += timedelta(days=1)
                            continue
                    if (
                        sfml_forecast_kwh is not None
                        or actual_kwh is not None
                        or external_1_kwh is not None
                        or (external_2_kwh is not None)
                    ):
                        sfml_accuracy = summary.get("accuracy_percent")
                        if sfml_accuracy is None:
                            sfml_accuracy = _compute_accuracy(
                                actual_kwh, sfml_forecast_kwh
                            )
                        external_1_accuracy = _compute_accuracy(
                            actual_kwh, external_1_kwh
                        )
                        external_2_accuracy = _compute_accuracy(
                            actual_kwh, external_2_kwh
                        )
                        best_source = _determine_best_source(
                            sfml_accuracy, external_1_accuracy, external_2_accuracy
                        )
                        await self._upsert_day(
                            connection,
                            day_str,
                            actual_kwh=actual_kwh,
                            sfml_forecast_kwh=sfml_forecast_kwh,
                            sfml_accuracy_percent=sfml_accuracy,
                            external_1_kwh=external_1_kwh,
                            external_1_accuracy_percent=external_1_accuracy,
                            external_2_kwh=external_2_kwh,
                            external_2_accuracy_percent=external_2_accuracy,
                            best_source=best_source,
                        )
                        days_collected += 1
                    current_date += timedelta(days=1)
        except Exception as error:
            _LOGGER.error(
                "No DB write transaction for historical collection: %s", error
            )
            return False
        _LOGGER.info(
            "Historical forecast comparison data collected: %d days added to DB",
            days_collected,
        )
        return True

    async def async_repair_missing_sfml_forecasts(self, days: int = 7) -> int:
        """Repair missing SFML forecasts from database. @zara"""
        _LOGGER.info("Repairing missing SFML forecasts for last %d days", days)
        sfml_summaries = await self._get_all_sfml_summaries()
        try:
            async with self._write_transaction() as connection:
                repaired_count = 0
                today_date = ha_today(self._hass)
                start_date = today_date - timedelta(days=days - 1)
                current_date = start_date
                while current_date <= today_date:
                    day_str = current_date.isoformat()
                    existing_day = await self._get_existing_day(connection, day_str)
                    if existing_day:
                        if existing_day.get("sfml_forecast_kwh") is None:
                            summary = sfml_summaries.get(day_str, {})
                            sfml_forecast_kwh = summary.get("predicted_kwh")
                            if sfml_forecast_kwh is not None:
                                actual_kwh = existing_day.get("actual_kwh")
                                sfml_accuracy = summary.get("accuracy_percent")
                                if sfml_accuracy is None:
                                    sfml_accuracy = _compute_accuracy(
                                        actual_kwh, sfml_forecast_kwh
                                    )
                                external_1_accuracy = existing_day.get(
                                    "external_1_accuracy_percent"
                                )
                                external_2_accuracy = existing_day.get(
                                    "external_2_accuracy_percent"
                                )
                                best_source = _determine_best_source(
                                    sfml_accuracy,
                                    external_1_accuracy,
                                    external_2_accuracy,
                                )
                                await self._upsert_day(
                                    connection,
                                    day_str,
                                    sfml_forecast_kwh=sfml_forecast_kwh,
                                    sfml_accuracy_percent=sfml_accuracy,
                                    best_source=best_source,
                                )
                                _LOGGER.info(
                                    "Repaired SFML forecast for %s: %.2f kWh",
                                    day_str,
                                    sfml_forecast_kwh,
                                )
                                repaired_count += 1
                    current_date += timedelta(days=1)
        except Exception as error:
            _LOGGER.error("No DB write transaction for forecast repair: %s", error)
            return 0
        if repaired_count > 0:
            _LOGGER.info("Repaired %d missing SFML forecasts in DB", repaired_count)
        return repaired_count

    @staticmethod
    def _calculate_lock_time(
        day_date: date,
        latitude: float | None,
        longitude: float | None,
        timezone_str: str | None,
    ) -> datetime | None:
        """Calculate dynamic lock time (45 minutes before sunrise) for a given date. @zara"""
        try:
            from astral import Observer as Observer
            from astral.sun import sun as get_sun_times
            from zoneinfo import ZoneInfo as ZoneInfo

            if latitude is None or longitude is None:
                _LOGGER.warning("Latitude/longitude not configured in Home Assistant")
                return
            observer = Observer(latitude=float(latitude), longitude=float(longitude))
            sunrise = get_sun_times(
                observer, date=day_date, tzinfo=ZoneInfo(timezone_str or "UTC")
            )["sunrise"]
            return sunrise - timedelta(minutes=45)
        except Exception as error:
            _LOGGER.warning(
                "Failed to calculate sunrise via astral for %s: %s",
                day_date.isoformat(),
                error,
            )
            return

    async def _async_calculate_lock_time(self, day_date: date) -> datetime | None:
        return await self._hass.async_add_executor_job(
            self._calculate_lock_time,
            day_date,
            self._hass.config.latitude,
            self._hass.config.longitude,
            self._hass.config.time_zone,
        )

    async def async_schedule_next_lock_job(self) -> None:
        """Schedule the next morning forecast collection job dynamically. @zara"""
        if self._cancel_job:
            self._cancel_job()
            self._cancel_job = None
        from homeassistant.helpers.event import (
            async_track_point_in_utc_time as track_point_in_time,
        )
        from homeassistant.util import dt as dt_util

        now = dt_util.now()
        now_utc = dt_util.as_utc(now)
        today_date = now.date()
        lock_time = await self._async_calculate_lock_time(today_date)
        if isinstance(lock_time, datetime) and dt_util.as_utc(lock_time) <= now_utc:
            tomorrow_date = today_date + timedelta(days=1)
            lock_time = await self._async_calculate_lock_time(tomorrow_date)
        if not isinstance(lock_time, datetime):
            _LOGGER.warning(
                "Could not calculate dynamic lock time, falling back to 06:15 local time"
            )
            lock_time = now.replace(hour=6, minute=15, second=0, microsecond=0)
            if lock_time <= now:
                lock_time += timedelta(days=1)
        lock_time_utc = dt_util.as_utc(lock_time)
        _LOGGER.info(
            "Scheduled next morning forecast collection for %s (dynamic lock time)",
            lock_time_utc.isoformat(),
        )

        async def _job_fired(now):
            _LOGGER.info(
                "Dynamic morning forecast collection job triggered at %s",
                ha_now(self._hass).isoformat(),
            )
            try:
                await self.async_collect_morning_forecasts()
            except Exception as error:
                _LOGGER.error("Error running scheduled morning collection: %s", error)
            finally:
                self._hass.async_create_task(self.async_schedule_next_lock_job())

        self._cancel_job = track_point_in_time(
            self._hass, action=_job_fired, point_in_time=lock_time_utc
        )

    def stop(self) -> None:
        """Stop scheduled jobs. @zara"""
        if self._cancel_job:
            try:
                self._cancel_job()
                _LOGGER.info("Cancelled dynamic morning forecast collection job")
            except Exception as error:
                _LOGGER.warning("Error cancelling dynamic job: %s", error)
            self._cancel_job = None
        return None if True else None

    async def _get_historical_sensor_value(
        self, entity_id: str, target_dt: datetime
    ) -> float | None:
        """Read sensor value from Home Assistant Recorder at a specific past datetime. @zara"""
        start_time = target_dt - timedelta(hours=2)
        end_time = target_dt
        try:
            from homeassistant.components.recorder import (
                get_instance as get_recorder_instance,
            )
            from homeassistant.components.recorder.history import (
                get_significant_states as recorder_get_significant_states,
            )
        except ImportError:
            _LOGGER.warning("Recorder component not available")
            return
        recorder = get_recorder_instance(self._hass)

        def _fetch():
            return recorder_get_significant_states(
                self._hass,
                start_time,
                end_time,
                [entity_id],
                include_start_time_state=True,
            )

        history = await recorder.async_add_executor_job(_fetch)
        states = history.get(entity_id, [])
        if not states:
            return
        matched_state = None
        for sensor_state in states:
            if sensor_state.last_changed <= target_dt:
                matched_state = sensor_state
            else:
                break
        if matched_state is None and len(states) > 0:
            matched_state = states[0]
        if matched_state is not None and matched_state.state not in (
            "unknown",
            "unavailable",
        ):
            try:
                return float(matched_state.state)
            except (ValueError, TypeError):
                pass
        return None if True else None

    async def async_migrate_historical_forecasts(self) -> None:
        """Migrate and correct historical forecast values using dynamic lock times from recorder. @zara"""
        _LOGGER.info("Starting historical forecast comparison database migration...")
        try:
            async with self._get_db_connection() as connection:
                if not connection:
                    _LOGGER.error("No DB connection for historical forecast migration")
                    return
                async with connection.execute(
                    "SELECT date, actual_kwh, sfml_forecast_kwh FROM stats_forecast_comparison"
                ) as cursor:
                    rows = await cursor.fetchall()
            if not rows:
                _LOGGER.info("No historical forecast comparison data found to migrate")
                return
            config = self._get_config()
            external_1_entity = config.get(CONF_FORECAST_ENTITY_1)
            external_2_entity = config.get(CONF_FORECAST_ENTITY_2)
            if not external_1_entity and (not external_2_entity):
                _LOGGER.info(
                    "No external forecast entities configured, skipping migration"
                )
                return
            from datetime import date as date_type

            updates = []
            for row in rows:
                day_str = row["date"]
                day_date = date_type.fromisoformat(day_str)
                lock_time = await self._async_calculate_lock_time(day_date)
                if not lock_time:
                    continue
                external_1_kwh = None
                if external_1_entity:
                    external_1_kwh = await self._get_historical_sensor_value(
                        external_1_entity, lock_time
                    )
                external_2_kwh = None
                if external_2_entity:
                    external_2_kwh = await self._get_historical_sensor_value(
                        external_2_entity, lock_time
                    )
                if not (external_1_kwh is None and external_2_kwh is None):
                    actual_kwh = row["actual_kwh"]
                    sfml_forecast_kwh = row["sfml_forecast_kwh"]
                    sfml_accuracy = (
                        _compute_accuracy(actual_kwh, sfml_forecast_kwh)
                        if actual_kwh is not None
                        else None
                    )
                    external_1_accuracy = (
                        _compute_accuracy(actual_kwh, external_1_kwh)
                        if actual_kwh is not None and external_1_kwh is not None
                        else None
                    )
                    external_2_accuracy = (
                        _compute_accuracy(actual_kwh, external_2_kwh)
                        if actual_kwh is not None and external_2_kwh is not None
                        else None
                    )
                    best_source = _determine_best_source(
                        sfml_accuracy, external_1_accuracy, external_2_accuracy
                    )
                    updates.append(
                        (
                            external_1_kwh,
                            external_1_accuracy,
                            external_2_kwh,
                            external_2_accuracy,
                            sfml_accuracy,
                            best_source,
                            day_str,
                        )
                    )
                    continue
            if updates:
                async with self._write_transaction() as connection:
                    for update_params in updates:
                        await connection.execute(
                            "UPDATE stats_forecast_comparison\n                               SET external_1_kwh = COALESCE(?, external_1_kwh),\n                                   external_1_accuracy_percent = COALESCE(?, external_1_accuracy_percent),\n                                   external_2_kwh = COALESCE(?, external_2_kwh),\n                                   external_2_accuracy_percent = COALESCE(?, external_2_accuracy_percent),\n                                   sfml_accuracy_percent = COALESCE(?, sfml_accuracy_percent),\n                                   best_source = ?,\n                                   updated_at = CURRENT_TIMESTAMP\n                               WHERE date = ?",
                            update_params,
                        )
                _LOGGER.info(
                    "Successfully corrected %d historical forecast days in DB",
                    len(updates),
                )
            else:
                _LOGGER.info("No historical forecast days needed correction")
        except Exception as error:
            _LOGGER.error(
                "Error migrating historical forecasts: %s", error, exc_info=True
            )
        return None if True else None
