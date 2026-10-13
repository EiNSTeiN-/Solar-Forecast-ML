# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""SFML Data Reader for SFML Stats. @zara"""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator
import aiosqlite
from .utils.time_utils import ha_today

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
_LOGGER = logging.getLogger(__name__)
SFML_DOMAIN = "solar_forecast_ml"


class SFMLDataReader:
    """Reads solar data from SFML database. @zara"""

    def __init__(self, hass: HomeAssistant | None) -> None:
        """Initialize the SFML Data Reader. @zara"""
        self.hass = hass
        if hass is not None:
            self._db_path = (
                Path(hass.config.path()) / "solar_forecast_ml" / "solar_forecast.db"
            )
        else:
            self._db_path = Path("/config/solar_forecast_ml/solar_forecast.db")
            _LOGGER.warning(
                "SFMLDataReader initialized with None hass - using default path"
            )
        self._sfml_config = None

    @asynccontextmanager
    async def _get_db(self) -> AsyncIterator[aiosqlite.Connection]:
        """Get DB connection via manager with direct fallback. @zara"""
        from .storage.db_connection_manager import (
            get_manager as get_manager,
            DatabaseConnectionManager as DatabaseConnectionManager,
        )

        connection_manager = get_manager()
        if connection_manager is not None:
            if connection_manager.is_connected:
                yield (await connection_manager.get_connection())
                return
        async with DatabaseConnectionManager.connect_direct_safe(
            self._db_path
        ) as connection:
            yield connection
        if True:
            return

    @property
    def is_available(self) -> bool:
        """Check if SFML database is available. @zara"""
        return self._db_path.exists()

    @staticmethod
    def _is_locked_error(err: BaseException) -> bool:
        error_text = str(err).lower()
        return "database is locked" in error_text or "database is busy" in error_text

    def _log_read_error(self, context: str, err: BaseException) -> None:
        """A lock is a transient condition shared with the SFML writer: the
        sensors keep their previous values, so it is not an error."""
        if self._is_locked_error(err):
            _LOGGER.debug(
                "%s skipped, DB locked (values keep last state): %s", context, err
            )
        else:
            _LOGGER.error("%s: %s", context, err)
        return None if True else None

    def _get_sfml_config(self) -> dict[str, Any] | None:
        """Get SFML config entry data. @zara"""
        if self._sfml_config is not None:
            return self._sfml_config
        if self.hass is None:
            return
        for config_entry in self.hass.config_entries.async_entries(SFML_DOMAIN):
            self._sfml_config = {**config_entry.data, **config_entry.options}
            return self._sfml_config

    async def get_live_state(self) -> dict[str, Any]:
        """Read current live state from SFML SOT. @zara"""
        if not self.is_available:
            return {}
        today_iso = ha_today(self.hass).isoformat()
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "SELECT date, total_power_w, total_actual_kwh, power_source,\n                              actual_source, groups_expected, groups_available,\n                              quality, updated_at\n                       FROM actual_live_state\n                       WHERE date = ?",
                    (today_iso,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    async with connection.execute(
                        "SELECT date, total_power_w, total_actual_kwh, power_source,\n                                  actual_source, groups_expected, groups_available,\n                                  quality, updated_at\n                           FROM actual_live_state\n                           ORDER BY date DESC LIMIT 1"
                    ) as cursor:
                        row = await cursor.fetchone()
            if row is None:
                return {}
            return {
                "date": row["date"],
                "total_power_w": row["total_power_w"],
                "total_actual_kwh": row["total_actual_kwh"],
                "power_source": row["power_source"],
                "actual_source": row["actual_source"],
                "groups_expected": row["groups_expected"],
                "groups_available": row["groups_available"],
                "quality": row["quality"],
                "updated_at": row["updated_at"],
            }
        except Exception as error:
            self._log_read_error("Error reading SFML live state", error)
            return {}

    async def get_live_power(self) -> float | None:
        """Get current solar power from SFML SOT. @zara"""
        live_state = await self.get_live_state()
        raw_power_w = live_state.get("total_power_w")
        if raw_power_w is None:
            return
        try:
            return max(0.0, float(raw_power_w))
        except (ValueError, TypeError):
            return

    async def get_live_yield(self) -> float | None:
        """Get current daily yield from SFML SOT. @zara"""
        live_state = await self.get_live_state()
        if live_state.get("date") != ha_today(self.hass).isoformat():
            return
        raw_yield_kwh = live_state.get("total_actual_kwh")
        if raw_yield_kwh is None:
            return
        try:
            return max(0.0, float(raw_yield_kwh))
        except (ValueError, TypeError):
            return

    async def get_live_panel_groups(self) -> list[dict[str, Any]]:
        """Get current panel-group power values from SFML SOT. @zara"""
        if not self.is_available:
            return []
        today_iso = ha_today(self.hass).isoformat()
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "SELECT group_name, power_w, actual_today_kwh, power_sensor,\n                              quality, updated_at\n                       FROM actual_live_panel_groups\n                       WHERE date = ?\n                       ORDER BY group_name",
                    (today_iso,),
                ) as cursor:
                    rows = await cursor.fetchall()
            panel_groups = []
            for group_id, row in enumerate(rows, 1):
                panel_groups.append(
                    {
                        "id": group_id,
                        "name": row["group_name"],
                        "power": max(0.0, float(row["power_w"] or 0.0)),
                        "actual_today_kwh": max(
                            0.0, float(row["actual_today_kwh"] or 0.0)
                        ),
                        "power_sensor": row["power_sensor"],
                        "quality": row["quality"],
                        "updated_at": row["updated_at"],
                    }
                )
            return panel_groups
        except Exception as error:
            self._log_read_error("Error reading SFML live panel groups", error)
            return []

    async def get_today_data(self) -> dict[str, Any]:
        """Get today's solar data from SFML database. @zara"""
        if not self.is_available:
            return {}
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT yield_today_kwh, yield_today_sensor,\n                           peak_today_power_w, peak_today_at,\n                           consumption_today_kwh, autarky_percent\n                    FROM daily_forecast_tracking\n                    WHERE id = 1\n                "
                ) as cursor:
                    row = await cursor.fetchone()
                if row:
                    return {
                        "yield_kwh": row["yield_today_kwh"],
                        "yield_sensor": row["yield_today_sensor"],
                        "peak_power_w": row["peak_today_power_w"],
                        "peak_at": row["peak_today_at"],
                        "consumption_kwh": row["consumption_today_kwh"],
                        "autarky_percent": row["autarky_percent"],
                    }
        except Exception as error:
            self._log_read_error("Error reading today's data from SFML DB", error)
        return {} if True else None

    async def get_yield_cache(self) -> dict[str, Any]:
        """Get yield cache with timestamp. @zara"""
        if not self.is_available:
            return {}
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT value, time, date FROM yield_cache WHERE id = 1\n                "
                ) as cursor:
                    row = await cursor.fetchone()
                if row:
                    return {
                        "value": row["value"],
                        "time": row["time"],
                        "date": row["date"],
                    }
        except Exception as error:
            self._log_read_error("Error reading yield cache from SFML DB", error)
        return {} if True else None

    async def get_daily_history(self, days: int = 30) -> list[dict[str, Any]]:
        """Get daily solar production history from SFML database. @zara"""
        if not self.is_available:
            return []
        cutoff = (ha_today(self.hass) - timedelta(days=days)).isoformat()
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT cache_date, group_name,\n                           prediction_total_kwh, actual_total_kwh\n                    FROM panel_group_daily_cache\n                    WHERE cache_date >= ?\n                    ORDER BY cache_date DESC, group_name\n                ",
                    (cutoff,),
                ) as cursor:
                    rows = await cursor.fetchall()
            daily_history_by_date = {}
            for row in rows:
                date_key = row["cache_date"]
                if date_key not in daily_history_by_date:
                    daily_history_by_date[date_key] = {
                        "date": date_key,
                        "actual_kwh": 0.0,
                        "predicted_kwh": 0.0,
                        "groups": {},
                    }
                actual_kwh = row["actual_total_kwh"] or 0
                predicted_kwh = row["prediction_total_kwh"] or 0
                daily_history_by_date[date_key]["actual_kwh"] += actual_kwh
                daily_history_by_date[date_key]["predicted_kwh"] += predicted_kwh
                daily_history_by_date[date_key]["groups"][row["group_name"]] = {
                    "actual_kwh": actual_kwh,
                    "predicted_kwh": predicted_kwh,
                }
            return list(daily_history_by_date.values())
        except Exception as error:
            self._log_read_error("Error reading daily history from SFML DB", error)
            return []

    async def get_daily_summaries(self, days: int = 30) -> list[dict[str, Any]]:
        """Get daily summaries from SFML database. @zara"""
        if not self.is_available:
            return []
        cutoff = (ha_today(self.hass) - timedelta(days=days)).isoformat()
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT date, predicted_total_kwh, actual_total_kwh,\n                           accuracy_percent, peak_power_w, peak_hour,\n                           production_hours\n                    FROM daily_summaries\n                    WHERE date >= ?\n                    ORDER BY date DESC\n                ",
                    (cutoff,),
                ) as cursor:
                    rows = await cursor.fetchall()
            return [
                {
                    "date": row["date"],
                    "predicted_kwh": row["predicted_total_kwh"],
                    "actual_kwh": row["actual_total_kwh"],
                    "accuracy_percent": row["accuracy_percent"],
                    "peak_power_w": row["peak_power_w"],
                    "peak_hour": row["peak_hour"],
                    "production_hours": row["production_hours"],
                }
                for row in rows
            ]
        except Exception as error:
            self._log_read_error("Error reading daily summaries from SFML DB", error)
            return []

    async def get_hourly_history(self, days: int = 7) -> list[dict[str, Any]]:
        """Get hourly solar production history from SFML database. @zara"""
        if not self.is_available:
            return []
        cutoff = (ha_today(self.hass) - timedelta(days=days)).isoformat()
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT cache_date, hour, group_name,\n                           prediction_kwh, actual_kwh\n                    FROM panel_group_daily_hourly\n                    WHERE cache_date >= ?\n                    ORDER BY cache_date DESC, hour DESC, group_name\n                ",
                    (cutoff,),
                ) as cursor:
                    rows = await cursor.fetchall()
            hourly_history_by_key = {}
            for row in rows:
                hour_key = f"{row['cache_date']}_{row['hour']:02d}"
                if hour_key not in hourly_history_by_key:
                    hourly_history_by_key[hour_key] = {
                        "date": row["cache_date"],
                        "hour": row["hour"],
                        "actual_kwh": 0.0,
                        "predicted_kwh": 0.0,
                        "groups": {},
                    }
                actual_kwh = row["actual_kwh"] or 0
                predicted_kwh = row["prediction_kwh"] or 0
                hourly_history_by_key[hour_key]["actual_kwh"] += actual_kwh
                hourly_history_by_key[hour_key]["predicted_kwh"] += predicted_kwh
                hourly_history_by_key[hour_key]["groups"][row["group_name"]] = {
                    "actual_kwh": actual_kwh,
                    "predicted_kwh": predicted_kwh,
                }
            return list(hourly_history_by_key.values())
        except Exception as error:
            self._log_read_error("Error reading hourly history from SFML DB", error)
            return []

    async def get_hourly_predictions(
        self, date: str | None = None
    ) -> list[dict[str, Any]]:
        """Get hourly predictions for a specific date from SFML database. @zara"""
        if not self.is_available:
            return []
        if date is None:
            date = ha_today(self.hass).isoformat()
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT target_hour, prediction_kwh, actual_kwh,\n                           accuracy_percent, confidence\n                    FROM hourly_predictions\n                    WHERE target_date = ?\n                    ORDER BY target_hour\n                ",
                    (date,),
                ) as cursor:
                    rows = await cursor.fetchall()
            return [
                {
                    "hour": row["target_hour"],
                    "predicted_kwh": row["prediction_kwh"],
                    "actual_kwh": row["actual_kwh"],
                    "accuracy_percent": row["accuracy_percent"],
                    "confidence": row["confidence"],
                }
                for row in rows
            ]
        except Exception as error:
            self._log_read_error("Error reading hourly predictions from SFML DB", error)
            return []

    def _pivot_panel_rows(
        self, rows, date_field: str = "target_date"
    ) -> list[dict[str, Any]]:
        """Pivot prediction_panel_groups rows into hourly yield dicts. @zara"""
        from collections import defaultdict as defaultdict

        groups_by_hour = defaultdict(dict)
        dates_by_hour = {}
        for row in rows:
            date_hour_key = (row[date_field], row["target_hour"])
            dates_by_hour[date_hour_key] = row[date_field]
            groups_by_hour[date_hour_key][row["group_name"]] = row["actual_kwh"] or 0.0
        hourly_yields = []
        for date_hour_key in sorted(groups_by_hour):
            group_yields = groups_by_hour[date_hour_key]
            hourly_yield = {
                "date": dates_by_hour[date_hour_key],
                "hour": date_hour_key[1],
                "yield_total_kwh": sum(group_yields.values()),
                "groups": group_yields,
            }
            hourly_yields.append(hourly_yield)
        return hourly_yields

    async def get_hourly_yield(
        self, date: str | None = None, hour: int | None = None
    ) -> list[dict[str, Any]]:
        """Get hourly solar yield from prediction_panel_groups. @zara"""
        if not self.is_available:
            return []
        if date is None:
            date = ha_today(self.hass).isoformat()
        try:
            async with self._get_db() as connection:
                query = "\n                    SELECT hp.target_date, hp.target_hour,\n                           ppg.group_name, ppg.actual_kwh\n                    FROM prediction_panel_groups ppg\n                    JOIN hourly_predictions hp ON hp.prediction_id = ppg.prediction_id\n                    WHERE hp.target_date = ?"
                query_parameters = [date]
                if hour is not None:
                    query += " AND hp.target_hour = ?"
                    query_parameters.append(hour)
                query += " ORDER BY hp.target_hour, ppg.group_name"
                async with connection.execute(query, query_parameters) as cursor:
                    rows = await cursor.fetchall()
            return self._pivot_panel_rows(rows)
        except Exception as error:
            self._log_read_error(
                "Error reading hourly yield from prediction_panel_groups", error
            )
            return []

    async def get_daily_yield_from_hourly(
        self, date: str | None = None
    ) -> dict[str, Any]:
        """Get daily yield totals by summing panel group actuals. @zara"""
        if not self.is_available:
            return {}
        if date is None:
            date = ha_today(self.hass).isoformat()
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT ppg.group_name,\n                           SUM(ppg.actual_kwh) as total\n                    FROM prediction_panel_groups ppg\n                    JOIN hourly_predictions hp ON hp.prediction_id = ppg.prediction_id\n                    WHERE hp.target_date = ?\n                    GROUP BY ppg.group_name\n                ",
                    (date,),
                ) as cursor:
                    rows = await cursor.fetchall()
            if not rows:
                return {}
            group_yields = {row["group_name"]: row["total"] or 0.0 for row in rows}
            return {
                "date": date,
                "yield_total_kwh": sum(group_yields.values()),
                "groups": group_yields,
            }
        except Exception as error:
            self._log_read_error(
                "Error reading daily yield from prediction_panel_groups", error
            )
        return {} if True else None

    async def get_hourly_yield_range(
        self, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """Get hourly yield for a date range from prediction_panel_groups. @zara"""
        if not self.is_available:
            return []
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT hp.target_date, hp.target_hour,\n                           ppg.group_name, ppg.actual_kwh\n                    FROM prediction_panel_groups ppg\n                    JOIN hourly_predictions hp ON hp.prediction_id = ppg.prediction_id\n                    WHERE hp.target_date >= ? AND hp.target_date <= ?\n                    ORDER BY hp.target_date, hp.target_hour, ppg.group_name\n                ",
                    (start_date, end_date),
                ) as cursor:
                    rows = await cursor.fetchall()
            return self._pivot_panel_rows(rows)
        except Exception as error:
            self._log_read_error("Error reading hourly yield range", error)
            return []

    async def get_panel_groups(self) -> list[dict[str, Any]]:
        """Get panel group configurations from SFML. @zara"""
        sfml_config = self._get_sfml_config()
        if not sfml_config:
            return []
        panel_groups_text = sfml_config.get("panel_groups", "")
        if not panel_groups_text:
            return []
        panel_groups = []
        for group_index, group_text in enumerate(panel_groups_text.split(",")):
            group_fields = group_text.strip().split("/")
            if len(group_fields) >= 3:
                group_config = {
                    "name": f"Gruppe {group_index + 1}",
                    "power_wp": float(group_fields[0]) if group_fields[0] else 0,
                    "azimuth": float(group_fields[1]) if group_fields[1] else 0,
                    "tilt": float(group_fields[2]) if group_fields[2] else 0,
                }
                if len(group_fields) >= 4 and group_fields[3]:
                    group_config["energy_sensor"] = group_fields[3]
                panel_groups.append(group_config)
        return panel_groups

    async def get_panel_group_sensor_states(self) -> dict[str, float]:
        """Get current sensor values for all panel groups. @zara"""
        if not self.is_available:
            return {}
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT group_name, last_value, last_updated\n                    FROM panel_group_sensor_state\n                "
                ) as cursor:
                    rows = await cursor.fetchall()
            return {
                row["group_name"]: {
                    "value": row["last_value"],
                    "last_updated": row["last_updated"],
                }
                for row in rows
            }
        except Exception as error:
            self._log_read_error(
                "Error reading panel group sensor states from SFML DB", error
            )
            return {}

    async def get_forecast(self) -> dict[str, Any]:
        """Get current forecast from SFML database. @zara"""
        if not self.is_available:
            return {}
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT forecast_type, forecast_date, prediction_kwh, locked\n                    FROM daily_forecasts\n                    ORDER BY created_at DESC\n                "
                ) as cursor:
                    rows = await cursor.fetchall()
            forecasts_by_type = {}
            for row in rows:
                forecast_type = row["forecast_type"]
                if forecast_type not in forecasts_by_type:
                    forecasts_by_type[forecast_type] = {
                        "date": row["forecast_date"],
                        "prediction_kwh": row["prediction_kwh"],
                        "locked": row["locked"],
                    }
            return forecasts_by_type
        except Exception as error:
            self._log_read_error("Error reading forecast from SFML DB", error)
            return {}

    async def get_statistics(self) -> dict[str, Any]:
        """Get solar statistics from SFML database. @zara"""
        if not self.is_available:
            return {}
        try:
            async with self._get_db() as connection:
                async with connection.execute(
                    "\n                    SELECT * FROM daily_statistics WHERE id = 1\n                "
                ) as cursor:
                    statistics_row = await cursor.fetchone()
                if not statistics_row:
                    return {}
                return {
                    "all_time_peak": {
                        "power_w": statistics_row["all_time_peak_power_w"],
                        "date": statistics_row["all_time_peak_date"],
                        "at": statistics_row["all_time_peak_at"],
                    },
                    "current_week": {
                        "period": statistics_row["current_week_period"],
                        "yield_kwh": statistics_row["current_week_yield_kwh"],
                        "consumption_kwh": statistics_row[
                            "current_week_consumption_kwh"
                        ],
                        "days": statistics_row["current_week_days"],
                    },
                    "current_month": {
                        "period": statistics_row["current_month_period"],
                        "yield_kwh": statistics_row["current_month_yield_kwh"],
                        "consumption_kwh": statistics_row[
                            "current_month_consumption_kwh"
                        ],
                        "avg_autarky": statistics_row["current_month_avg_autarky"],
                        "days": statistics_row["current_month_days"],
                    },
                    "last_7_days": {
                        "avg_yield_kwh": statistics_row["last_7_days_avg_yield_kwh"],
                        "avg_accuracy": statistics_row["last_7_days_avg_accuracy"],
                        "total_yield_kwh": statistics_row[
                            "last_7_days_total_yield_kwh"
                        ],
                    },
                    "last_30_days": {
                        "avg_yield_kwh": statistics_row["last_30_days_avg_yield_kwh"],
                        "avg_accuracy": statistics_row["last_30_days_avg_accuracy"],
                        "total_yield_kwh": statistics_row[
                            "last_30_days_total_yield_kwh"
                        ],
                    },
                }
        except Exception as error:
            self._log_read_error("Error reading statistics from SFML DB", error)
            return {}
        return None if True else None

    async def get_all_solar_data(self) -> dict[str, Any]:
        """Get all solar data in one call. @zara"""
        return {
            "live": {
                "power_w": await self.get_live_power(),
                "yield_kwh": await self.get_live_yield(),
                "state": await self.get_live_state(),
                "panel_groups": await self.get_live_panel_groups(),
                "today": await self.get_today_data(),
            },
            "history": {
                "daily": await self.get_daily_history(days=30),
                "hourly": await self.get_hourly_history(days=7),
            },
            "forecast": await self.get_forecast(),
            "statistics": await self.get_statistics(),
            "panel_groups": await self.get_panel_groups(),
            "sfml_available": self.is_available,
        }
