# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import aiofiles
import aiosqlite
import asyncio
import json
import logging
import random
from datetime import datetime, date
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple


class DatabaseManager:

    async def _migrate_visibility_learning_table_v161(self) -> None:
        """Create/update visibility_learning table. @zara V16.1

        This table stores fog detection learning data.
        V16.1 adds: visibility_threshold_m, fog_visibility_threshold_m,
                    samples_below_threshold, samples_above_threshold
        """
        async with self._db.execute(
            "SELECT name FROM sqlite_master\n               WHERE type='table' AND name='visibility_learning'"
        ) as _var_var_0:
            _var_var_1 = await _var_var_0.fetchone()
        if not _var_var_1:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS visibility_learning (\n                    id INTEGER PRIMARY KEY CHECK (id = 1),\n                    version TEXT DEFAULT '1.0',\n                    has_solar_radiation_sensor BOOLEAN DEFAULT FALSE,\n                    last_learning_date DATE,\n                    total_fog_hours_learned INTEGER DEFAULT 0,\n                    total_fog_light_hours_learned INTEGER DEFAULT 0,\n                    bright_sky_fog_hits INTEGER DEFAULT 0,\n                    pirate_weather_fog_hits INTEGER DEFAULT 0,\n                    learning_sessions INTEGER DEFAULT 0,\n                    fog_bright_sky_weight REAL DEFAULT 0.5,\n                    fog_pirate_weather_weight REAL DEFAULT 0.5,\n                    fog_light_bright_sky_weight REAL DEFAULT 0.5,\n                    fog_light_pirate_weather_weight REAL DEFAULT 0.5,\n                    visibility_threshold_m REAL DEFAULT 5000,\n                    fog_visibility_threshold_m REAL DEFAULT 1000,\n                    samples_below_threshold INTEGER DEFAULT 0,\n                    samples_above_threshold INTEGER DEFAULT 0,\n                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.1: Created visibility_learning table")
        else:
            async with self._db.execute(
                "PRAGMA table_info(visibility_learning)"
            ) as _var_var_0:
                _var_var_6 = await _var_var_0.fetchall()
                _var_var_10 = {_var_var_9[1] for _var_var_9 in _var_var_6}
            _var_var_13 = []
            if "visibility_threshold_m" not in _var_var_10:
                _var_var_13.append(
                    "ALTER TABLE visibility_learning ADD COLUMN visibility_threshold_m REAL DEFAULT 5000"
                )
            if "fog_visibility_threshold_m" not in _var_var_10:
                _var_var_13.append(
                    "ALTER TABLE visibility_learning ADD COLUMN fog_visibility_threshold_m REAL DEFAULT 1000"
                )
            if "samples_below_threshold" not in _var_var_10:
                _var_var_13.append(
                    "ALTER TABLE visibility_learning ADD COLUMN samples_below_threshold INTEGER DEFAULT 0"
                )
            if "samples_above_threshold" not in _var_var_10:
                _var_var_13.append(
                    "ALTER TABLE visibility_learning ADD COLUMN samples_above_threshold INTEGER DEFAULT 0"
                )
            if _var_var_13:
                for _var_var_14 in _var_var_13:
                    await self._db.execute(_var_var_14)
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.1: Added %d columns to visibility_learning",
                    len(_var_var_13),
                )
        if True:
            return

    async def _migrate_snow_tracking_table_v161(self) -> None:
        """Create/update snow_tracking table. @zara V16.1

        This table stores snow coverage tracking data.
        V16.1 adds: melt_hours, detection_source, cleared_at
        """
        async with self._db.execute(
            "SELECT name FROM sqlite_master\n               WHERE type='table' AND name='snow_tracking'"
        ) as _var_var_0:
            _var_var_1 = await _var_var_0.fetchone()
        if not _var_var_1:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS snow_tracking (\n                    id INTEGER PRIMARY KEY CHECK (id = 1),\n                    last_snow_event TIMESTAMP,\n                    panels_covered_since TIMESTAMP,\n                    estimated_depth_mm REAL DEFAULT 0,\n                    melt_started_at TIMESTAMP,\n                    melt_hours REAL DEFAULT 0,\n                    detection_source TEXT,\n                    cleared_at TIMESTAMP,\n                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n                )"
            )
            await self._db.commit()
            _LOGGER.info("Migration V16.1: Created snow_tracking table")
        else:
            async with self._db.execute(
                "PRAGMA table_info(snow_tracking)"
            ) as _var_var_0:
                _var_var_6 = await _var_var_0.fetchall()
                _var_var_10 = {_var_var_9[1] for _var_var_9 in _var_var_6}
            _var_var_13 = []
            if "melt_hours" not in _var_var_10:
                _var_var_13.append(
                    "ALTER TABLE snow_tracking ADD COLUMN melt_hours REAL DEFAULT 0"
                )
            if "detection_source" not in _var_var_10:
                _var_var_13.append(
                    "ALTER TABLE snow_tracking ADD COLUMN detection_source TEXT"
                )
            if "cleared_at" not in _var_var_10:
                _var_var_13.append(
                    "ALTER TABLE snow_tracking ADD COLUMN cleared_at TIMESTAMP"
                )
            if _var_var_13:
                for _var_var_14 in _var_var_13:
                    await self._db.execute(_var_var_14)
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.1: Added %d columns to snow_tracking",
                    len(_var_var_13),
                )
        if True:
            return

    async def _backfill_astronomy_enrichment_v1640(self) -> None:
        """V16.4.0: Backfill prediction_astronomy enrichment from stored sunrise/sunset/noon. @zara"""
        try:
            async with self._db.execute(
                "SELECT pa.prediction_id, pa.sunrise, pa.sunset, pa.solar_noon, hp.target_hour\n                   FROM prediction_astronomy pa\n                   JOIN hourly_predictions hp ON hp.prediction_id = pa.prediction_id\n                   WHERE (pa.day_progress_ratio IS NULL OR pa.hours_after_sunrise IS NULL\n                          OR pa.hours_before_sunset IS NULL OR pa.hours_since_solar_noon IS NULL)\n                     AND pa.sunrise IS NOT NULL AND pa.sunset IS NOT NULL\n                   LIMIT 5000"
            ) as _var_var_0:
                _var_var_43 = await _var_var_0.fetchall()
            if not _var_var_43:
                return
            _var_var_44 = 0
            for _var_var_5 in _var_var_43:
                _var_var_45 = _var_var_5[0]
                _var_var_46 = _var_var_5[1]
                _var_var_47 = _var_var_5[2]
                _var_var_48 = _var_var_5[3]
                _var_var_49 = _var_var_5[4] if _var_var_5[4] is not None else 12
                try:
                    _var_var_50 = self._parse_astronomy_ts(_var_var_46)
                    _var_var_51 = self._parse_astronomy_ts(_var_var_47)
                    if _var_var_50 is None or _var_var_51 is None:
                        continue
                    sunrise_naive = (
                        _var_var_50.replace(tzinfo=None)
                        if _var_var_50.tzinfo
                        else _var_var_50
                    )
                    sunset_naive = (
                        _var_var_51.replace(tzinfo=None)
                        if _var_var_51.tzinfo
                        else _var_var_51
                    )
                    hour_midpoint = sunrise_naive.replace(
                        hour=_var_var_49, minute=30, second=0, microsecond=0
                    )
                    duration_seconds = (sunset_naive - sunrise_naive).total_seconds()
                    elapsed_seconds = (hour_midpoint - sunrise_naive).total_seconds()
                    day_progress = (
                        max(0.0, min(1.0, elapsed_seconds / duration_seconds))
                        if duration_seconds > 0
                        else 0.5
                    )
                    after_sunrise = max(0.0, elapsed_seconds / 3600.0)
                    before_sunset = max(
                        0.0, (sunset_naive - hour_midpoint).total_seconds() / 3600.0
                    )
                    since_noon = None
                    if _var_var_48:
                        noon_parsed = self._parse_astronomy_ts(_var_var_48)
                        if noon_parsed:
                            noon_naive = (
                                noon_parsed.replace(tzinfo=None)
                                if noon_parsed.tzinfo
                                else noon_parsed
                            )
                            since_noon = (
                                hour_midpoint - noon_naive
                            ).total_seconds() / 3600.0
                    await self._db.execute(
                        "UPDATE prediction_astronomy\n                           SET day_progress_ratio = ?,\n                               hours_after_sunrise = ?,\n                               hours_before_sunset = ?,\n                               hours_since_solar_noon = ?\n                           WHERE prediction_id = ?\n                             AND day_progress_ratio IS NULL",
                        (
                            round(day_progress, 6),
                            round(after_sunrise, 4),
                            round(before_sunset, 4),
                            round(since_noon, 4) if since_noon is not None else None,
                            _var_var_45,
                        ),
                    )
                    _var_var_44 += 1
                except Exception:
                    continue
            if _var_var_44 > 0:
                await self._db.commit()
                _LOGGER.info(
                    "Migration V16.4.0: Backfilled %d prediction_astronomy enrichment rows",
                    _var_var_44,
                )
        except Exception:
            pass
        return None if True else None

    @staticmethod
    def _parse_astronomy_ts(ts_str):
        """Parse astronomy timestamp with timezone offset support. @zara V16.4.0"""
        if not ts_str:
            return
        try:
            _var_var_70 = str(ts_str).strip()
            return datetime.fromisoformat(_var_var_70.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            for _var_var_71 in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S",
                "%H:%M:%S",
                "%H:%M",
            ):
                try:
                    return datetime.strptime(_var_var_70, _var_var_71)
                except (ValueError, TypeError):
                    continue
        if True:
            return

    async def save_weather_mlp_weights(self, weights_data: Dict[str, Any]) -> None:
        """Save Weather MLP weights to database. @zara"""
        try:
            await self._db.execute(
                "INSERT INTO ai_weather_mlp_meta\n                   (id, input_size, hidden1, hidden2, training_samples, accuracy, rmse,\n                    last_trained, updated_at)\n                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       input_size = excluded.input_size,\n                       hidden1 = excluded.hidden1,\n                       hidden2 = excluded.hidden2,\n                       training_samples = excluded.training_samples,\n                       accuracy = excluded.accuracy,\n                       rmse = excluded.rmse,\n                       last_trained = excluded.last_trained,\n                       updated_at = excluded.updated_at",
                (
                    weights_data.get("input_size", 8),
                    weights_data.get("hidden1", 16),
                    weights_data.get("hidden2", 8),
                    weights_data.get("training_samples", 0),
                    weights_data.get("accuracy", 0.0),
                    weights_data.get("rmse", 0.0),
                    datetime.now(),
                    datetime.now(),
                ),
            )
            await self._db.execute("DELETE FROM ai_weather_mlp_weights")
            for _var_var_90, _var_var_91 in weights_data.items():
                if _var_var_90 in (
                    "input_size",
                    "hidden1",
                    "hidden2",
                    "training_samples",
                    "accuracy",
                    "rmse",
                ):
                    continue
                if not isinstance(_var_var_91, (list,)):
                    continue
                _var_var_94 = _var_var_91
                if len(_var_var_94) > 0 and isinstance(_var_var_94[0], list):
                    _var_var_94 = [
                        _var_var_149
                        for _var_var_5 in _var_var_91
                        for _var_var_149 in _var_var_5
                    ]
                for _var_var_95, _var_var_93 in enumerate(_var_var_94):
                    await self._db.execute(
                        "INSERT OR REPLACE INTO ai_weather_mlp_weights (weight_type, weight_index, weight_value) VALUES (?, ?, ?)",
                        (_var_var_90, _var_var_95, float(_var_var_93)),
                    )
            await self._db.commit()
            _LOGGER.info(
                "Weather MLP weights saved: %d samples, R²=%.3f",
                weights_data.get("training_samples", 0),
                weights_data.get("accuracy", 0.0),
            )
        except Exception as _var_var_11:
            _LOGGER.error("Failed to save Weather MLP weights: %s", _var_var_11)
        return None if True else None

    async def _retry_on_locked(self, operation, max_retries: int = 5):
        """Retry a DB operation on 'database is locked' with exponential backoff. @zara"""
        for _var_var_176 in range(max_retries + 1):
            try:
                return await operation()
            except Exception as _var_var_11:
                _var_var_177 = str(_var_var_11).lower()
                _var_var_178 = (
                    "database is locked" in _var_var_177
                    or "database is busy" in _var_var_177
                )
                if _var_var_178 and _var_var_176 < max_retries:
                    _var_var_179 = min(0.2 * 3**_var_var_176, 5.0) + random.uniform(
                        0, 0.1
                    )
                    _LOGGER.warning(
                        "DB locked (attempt %d/%d), retrying in %.2fs: %s",
                        _var_var_176 + 1,
                        max_retries,
                        _var_var_179,
                        str(_var_var_11)[:100],
                    )
                    await (asyncio.sleep if True else None)(_var_var_179)
                    continue
                if _var_var_178:
                    _LOGGER.error(
                        "DB locked after %d retries, giving up: %s",
                        max_retries,
                        str(_var_var_11)[:200],
                    )
                raise

    async def execute(
        self, sql: str, parameters: tuple = (), auto_commit: bool = True
    ) -> None:
        """Execute a raw SQL statement with write serialization and retry on lock. @zara"""
        if self._db is None:
            _LOGGER.debug("Database is closed, skipping execute")
            return
        _var_var_181 = asyncio.current_task()
        has_transaction = self._transaction_owner is _var_var_181

        async def _do():
            _var_var_183 = self._transaction_db if has_transaction else self._db
            await _var_var_183.execute(sql, parameters)
            if auto_commit and (not has_transaction):
                await _var_var_183.commit()
            return None if True else None

        if has_transaction:
            await self._retry_on_locked(_do)
        elif auto_commit and self._write_lock is not None:
            async with self._write_lock:
                await self._retry_on_locked(_do)
        else:
            await self._retry_on_locked(_do)
        return None if True else None

    @asynccontextmanager
    async def transaction(self):
        """Context manager for multi-statement write transactions. @zara

        Holds the write lock for the entire scope, ensuring all
        auto_commit=False execute() calls and the final commit()
        are serialized as one atomic unit against other writers.

        Usage:
            async with db.transaction():
                await db.execute("INSERT ...", params, auto_commit=False)
                await db.execute("UPDATE ...", params, auto_commit=False)
                await db.commit()
        """
        if self._db is None or self._write_lock is None:
            raise RuntimeError("Database is not connected")
        _var_var_181 = asyncio.current_task()
        if self._transaction_owner is _var_var_181:
            self._savepoint_counter += 1
            _var_var_184 = f"eai_nested_{self._savepoint_counter}"
            self._transaction_depth += 1
            await self._transaction_db.execute(f"SAVEPOINT {_var_var_184}")
            try:
                yield None
            except BaseException:
                await self._transaction_db.execute(
                    f"ROLLBACK TO SAVEPOINT {_var_var_184}"
                )
                await self._transaction_db.execute(f"RELEASE SAVEPOINT {_var_var_184}")
                raise
            else:
                await self._transaction_db.execute(f"RELEASE SAVEPOINT {_var_var_184}")
            finally:
                self._transaction_depth -= 1
            return
        await self._write_lock.acquire()
        self._transaction_owner = _var_var_181
        self._transaction_depth = 1
        try:
            self._transaction_db = await aiosqlite.connect(self.db_path)
            await self._transaction_db.execute("PRAGMA foreign_keys = ON")
            await self._transaction_db.execute("PRAGMA busy_timeout = 60000")
            await self._retry_on_locked(
                lambda: self._transaction_db.execute("BEGIN IMMEDIATE")
            )
            try:
                yield None
            except BaseException:
                await self._transaction_db.rollback()
                raise
            else:
                await self._transaction_db.commit()
        finally:
            if self._transaction_db is not None:
                await self._transaction_db.close()
                self._transaction_db = None
            self._transaction_depth = 0
            self._transaction_owner = None
            self._write_lock.release()
