# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""Durable EAI consumption-learning state in the SFML central database.

EAI owns the two ``eai_`` tables through the SFML database-service lease.  The
legacy EAI database keeps its existing model weights, but is never used as the
durable measurement baseline or training-sample source.
"""
from __future__ import annotations
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

_LOGGER = logging.getLogger(__name__)
_SAMPLE_TABLE = "eai_consumption_training_samples"
_STATE_TABLE = "eai_consumption_writer_state"
_OPERATION_TIMEOUT_MS = 10000
_TRANSIENT_RETRY_DELAYS = (0.5, 1.0, 2.0)
_T = TypeVar("_T")


class EAIConsumptionSampleStore:
    """Access EAI-owned learning state via the SFML central DB capability."""

    def __init__(self, hass: Any, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._service = None
        self._lease = None
        self._unavailable_logged = False

    @staticmethod
    def _manifest() -> Any:
        """Build the narrow EAI operation manifest lazily.

        Importing the contract lazily keeps cleartext unit tests independent of
        a Home Assistant package loader while production always obtains the
        public SFML capability, never a SQLite connection.
        """
        from custom_components.solar_forecast_ml.core.database_service import (
            DomainManifest as DomainManifest,
            IdempotencyMode as IdempotencyMode,
            ObjectSpec as ObjectSpec,
            OperationSpec as OperationSpec,
            StatementSpec as StatementSpec,
            WritePriority as WritePriority,
        )

        sample_tables = frozenset({_SAMPLE_TABLE})
        state_tables = frozenset({_STATE_TABLE})
        write_tables = sample_tables | state_tables
        return DomainManifest(
            domain_id="eai",
            owner_repository="05_solar_forecast_eai",
            repository_version="1.0.0",
            service_api=">=1.0.0,<2.0.0",
            schema_version=1,
            objects=(
                ObjectSpec(
                    name=_SAMPLE_TABLE,
                    writer_domain="eai",
                    access_mode="private",
                    retention_class="durable",
                ),
                ObjectSpec(
                    name=_STATE_TABLE,
                    writer_domain="eai",
                    access_mode="private",
                    retention_class="durable",
                ),
            ),
            operations=(
                OperationSpec(
                    operation_id="record_meter_interval",
                    write_tables=write_tables,
                    priority=WritePriority.CORE,
                    timeout_ms=_OPERATION_TIMEOUT_MS,
                    idempotency=IdempotencyMode.REQUIRED,
                    statements=(
                        StatementSpec(
                            "delete_sample",
                            f"DELETE FROM {_SAMPLE_TABLE} WHERE sample_key = ?",
                            ("sample_key",),
                            sample_tables,
                        ),
                        StatementSpec(
                            "insert_sample",
                            f"INSERT INTO {_SAMPLE_TABLE} (sample_key, entry_id, target_date, target_hour, actual_kwh, feature_snapshot, accepted_at, source, excluded) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                "sample_key",
                                "entry_id",
                                "target_date",
                                "target_hour",
                                "actual_kwh",
                                "feature_snapshot",
                                "accepted_at",
                                "source",
                                "excluded",
                            ),
                            sample_tables,
                        ),
                        StatementSpec(
                            "delete_state",
                            f"DELETE FROM {_STATE_TABLE} WHERE entry_id = ?",
                            ("entry_id",),
                            state_tables,
                        ),
                        StatementSpec(
                            "insert_state",
                            f"INSERT INTO {_STATE_TABLE} (entry_id, baseline_kwh, baseline_observed_at_utc, baseline_local_date, target_entity_id, target_unit, measurement_boundary_fingerprint, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                "entry_id",
                                "baseline_kwh",
                                "baseline_observed_at_utc",
                                "baseline_local_date",
                                "target_entity_id",
                                "target_unit",
                                "measurement_boundary_fingerprint",
                                "updated_at_utc",
                            ),
                            state_tables,
                        ),
                    ),
                ),
                OperationSpec(
                    operation_id="replace_training_sample",
                    write_tables=sample_tables,
                    priority=WritePriority.NORMAL,
                    timeout_ms=_OPERATION_TIMEOUT_MS,
                    idempotency=IdempotencyMode.REQUIRED,
                    statements=(
                        StatementSpec(
                            statement_id="delete_existing",
                            sql=f"DELETE FROM {_SAMPLE_TABLE} WHERE sample_key = ?",
                            parameters=("sample_key",),
                            write_tables=sample_tables,
                        ),
                        StatementSpec(
                            statement_id="insert_sample",
                            sql=f"INSERT INTO {_SAMPLE_TABLE} (sample_key, entry_id, target_date, target_hour, actual_kwh, feature_snapshot, accepted_at, source, excluded) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            parameters=(
                                "sample_key",
                                "entry_id",
                                "target_date",
                                "target_hour",
                                "actual_kwh",
                                "feature_snapshot",
                                "accepted_at",
                                "source",
                                "excluded",
                            ),
                            write_tables=sample_tables,
                        ),
                    ),
                ),
                OperationSpec(
                    operation_id="load_training_samples",
                    write_tables=frozenset(),
                    read_tables=sample_tables,
                    timeout_ms=_OPERATION_TIMEOUT_MS,
                    idempotency=IdempotencyMode.FORBIDDEN,
                    statements=(
                        StatementSpec(
                            statement_id="select_samples",
                            sql=f"SELECT feature_snapshot FROM {_SAMPLE_TABLE} WHERE entry_id = ? AND target_date >= ? AND target_date < ? AND excluded = ? ORDER BY target_date, target_hour, accepted_at",
                            parameters=("entry_id", "cutoff", "before", "excluded"),
                            read_tables=sample_tables,
                        ),
                    ),
                ),
                OperationSpec(
                    operation_id="replace_writer_state",
                    write_tables=state_tables,
                    priority=WritePriority.CORE,
                    timeout_ms=_OPERATION_TIMEOUT_MS,
                    idempotency=IdempotencyMode.REQUIRED,
                    statements=(
                        StatementSpec(
                            statement_id="delete_existing",
                            sql=f"DELETE FROM {_STATE_TABLE} WHERE entry_id = ?",
                            parameters=("entry_id",),
                            write_tables=state_tables,
                        ),
                        StatementSpec(
                            statement_id="insert_state",
                            sql=f"INSERT INTO {_STATE_TABLE} (entry_id, baseline_kwh, baseline_observed_at_utc, baseline_local_date, target_entity_id, target_unit, measurement_boundary_fingerprint, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            parameters=(
                                "entry_id",
                                "baseline_kwh",
                                "baseline_observed_at_utc",
                                "baseline_local_date",
                                "target_entity_id",
                                "target_unit",
                                "measurement_boundary_fingerprint",
                                "updated_at_utc",
                            ),
                            write_tables=state_tables,
                        ),
                    ),
                ),
                OperationSpec(
                    operation_id="load_writer_state",
                    write_tables=frozenset(),
                    read_tables=state_tables,
                    priority=WritePriority.CORE,
                    timeout_ms=_OPERATION_TIMEOUT_MS,
                    idempotency=IdempotencyMode.FORBIDDEN,
                    statements=(
                        StatementSpec(
                            statement_id="select_state",
                            sql=f"SELECT baseline_kwh, baseline_observed_at_utc, baseline_local_date, target_entity_id, target_unit, measurement_boundary_fingerprint FROM {_STATE_TABLE} WHERE entry_id = ?",
                            parameters=("entry_id",),
                            read_tables=state_tables,
                        ),
                    ),
                ),
            ),
        )

    async def _async_ready(self) -> bool:
        if self._lease is not None and self._service is not None:
            return True
        try:
            from custom_components.solar_forecast_ml.core.database_service import (
                async_wait_for_database_service as async_wait_for_database_service,
            )

            self._service = await async_wait_for_database_service(
                self._hass, timeout=5.0
            )
            self._lease = await self._service.register_domain(self._manifest())
        except Exception as error:
            self._service = None
            self._lease = None
            if not self._unavailable_logged:
                _LOGGER.info(
                    "SFML central EAI learning store is not ready yet; measurement baseline will be re-established safely: %s",
                    error,
                )
                self._unavailable_logged = True
            return False
        return True

    async def async_available(self) -> bool:
        """Return whether the SFML-owned EAI persistence capability is ready."""
        return await self._async_ready()

    @staticmethod
    def _is_transient_database_error(error: Exception) -> bool:
        return (
            bool(getattr(error, "retryable", False))
            or getattr(error, "code", None) == "transaction_timeout"
        )

    async def _async_call_with_retry(
        self, operation: Callable[[], Awaitable[_T]]
    ) -> _T:
        """Retry bounded central-DB contention without weakening fail-closed writes."""
        for attempt in range(len(_TRANSIENT_RETRY_DELAYS) + 1):
            try:
                return await operation()
            except Exception as error:
                if attempt >= len(
                    _TRANSIENT_RETRY_DELAYS
                ) or not self._is_transient_database_error(error):
                    raise
                _LOGGER.warning(
                    "Retrying EAI central database operation after %s", error
                )
                await asyncio.sleep(_TRANSIENT_RETRY_DELAYS[attempt])
                continue
        raise RuntimeError("unreachable central database retry state")

    async def async_load_writer_state(
        self, source_entity_id: str, source_unit: str, boundary_fingerprint: str
    ) -> dict[str, Any] | None:
        if not await self._async_ready():
            return
        try:
            rows = await self._async_call_with_retry(
                lambda: self._service.fetch_domain_operation(
                    self._lease, "load_writer_state", {"entry_id": self._entry_id}
                )
            )
            if not rows:
                return
            row = rows[0]
            values = row.values() if isinstance(row, dict) else row
            (
                baseline_kwh,
                observed_at_utc,
                local_date,
                target_entity_id,
                target_unit,
                measurement_boundary_fingerprint,
            ) = values
            if (target_entity_id, target_unit, measurement_boundary_fingerprint) != (
                source_entity_id,
                source_unit,
                boundary_fingerprint,
            ):
                _LOGGER.info(
                    "EAI measurement baseline rebaselined after source entity change"
                )
                return
            return {
                "value": float(baseline_kwh),
                "time": str(observed_at_utc),
                "date": str(local_date),
            }
        except Exception as error:
            _LOGGER.warning("Could not load EAI measurement baseline: %s", error)
            return

    async def async_save_writer_state(
        self,
        *,
        value: float,
        time_local: str,
        date_local: str,
        source_entity_id: str,
        source_unit: str,
        boundary_fingerprint: str,
    ) -> bool:
        if not await self._async_ready():
            return False
        try:
            observed_at_utc = (
                datetime.fromisoformat(time_local)
                .astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except ValueError:
            _LOGGER.warning("Refusing malformed EAI measurement baseline timestamp")
            return False
        operation_parameters = {
            "entry_id": self._entry_id,
            "baseline_kwh": float(value),
            "baseline_observed_at_utc": observed_at_utc,
            "baseline_local_date": date_local,
            "target_entity_id": source_entity_id,
            "target_unit": source_unit,
            "measurement_boundary_fingerprint": boundary_fingerprint,
            "updated_at_utc": observed_at_utc,
        }
        try:
            await self._async_call_with_retry(
                lambda: self._service.execute_domain_operation(
                    self._lease,
                    "replace_writer_state",
                    operation_parameters,
                    idempotency_key=f"baseline:{self._entry_id}:{date_local}:{time_local}:{value}",
                )
            )
        except Exception as error:
            _LOGGER.warning("Could not persist EAI measurement baseline: %s", error)
            return False
        return True

    async def async_store_sample(
        self, snapshot: dict[str, Any], *, source: str
    ) -> bool:
        """Persist one complete, accepted hourly sample atomically and idempotently."""
        target_date = snapshot.get("target_date")
        target_hour = snapshot.get("target_hour")
        actual_kwh = snapshot.get("actual_kwh")
        if not (
            isinstance(target_date, str)
            and isinstance(target_hour, int)
            and (0 <= target_hour <= 23)
        ):
            return False if True else None
        if not isinstance(actual_kwh, (int, float)) or actual_kwh < 0:
            return False
        if not not (snapshot.get("flags") or {}).get("is_outlier") or (
            snapshot.get("flags") or {}
        ).get("inverter_clipped"):
            return False
        if not await self._async_ready():
            return False
        operation_sample_key = f"{self._entry_id}:{target_date}:{target_hour:02d}"
        operation_parameters = {
            "sample_key": operation_sample_key,
            "entry_id": self._entry_id,
            "target_date": target_date,
            "target_hour": target_hour,
            "actual_kwh": float(actual_kwh),
            "feature_snapshot": json.dumps(
                snapshot, sort_keys=True, separators=(",", ":")
            ),
            "accepted_at": str(
                snapshot.get("accepted_at") or f"{target_date}T{target_hour:02d}:59:59"
            ),
            "source": source,
            "excluded": 0,
        }
        try:
            await self._async_call_with_retry(
                lambda: self._service.execute_domain_operation(
                    self._lease,
                    "replace_training_sample",
                    operation_parameters,
                    idempotency_key=f"sample:{operation_sample_key}",
                )
            )
        except Exception as error:
            _LOGGER.warning(
                "Could not persist EAI learning sample %s: %s",
                operation_sample_key,
                error,
            )
            return False
        return True

    async def async_store_meter_sample(
        self,
        *,
        date: str,
        hour: int,
        actual_kwh: float,
        sensor_data: dict[str, Any],
        weather_actual: dict[str, Any],
        astronomy: dict[str, Any],
        baseline_kwh: float,
        baseline_time_local: str,
        baseline_date_local: str,
        source_entity_id: str,
        source_unit: str,
        boundary_fingerprint: str,
        interval_start_utc: str,
        interval_end_utc: str,
    ) -> bool:
        """Persist the valid meter interval even when a legacy forecast row is absent.

        The snapshot is deliberately sourced from the completed interval and
        SFML astronomy.  A legacy EAI prediction row is not a prerequisite for
        learning persistence.
        """
        snapshot = {
            "target_date": date,
            "target_hour": hour,
            "interval_start_utc": interval_start_utc,
            "interval_end_utc": interval_end_utc,
            "actual_kwh": actual_kwh,
            "panel_group_actuals": {},
            "weather_corrected": {
                "temperature": sensor_data.get("temperature_c"),
                "solar_radiation_wm2": sensor_data.get("solar_radiation_wm2"),
                "wind": sensor_data.get("wind_speed_ms"),
                "humidity": sensor_data.get("humidity_percent"),
                "rain": sensor_data.get("rain_mm"),
                "clouds": weather_actual.get("cloud_cover_percent"),
                "pressure": sensor_data.get("pressure_hpa"),
                "diffuse_radiation": weather_actual.get("diffuse_radiation"),
                "dni": weather_actual.get("direct_radiation"),
            },
            "astronomy": dict(astronomy or {}),
            "physics_kwh": None,
            "ai_kwh": None,
            "ai_confidence_stored": None,
            "prediction_kwh": None,
            "accepted_at": interval_end_utc,
            "flags": {"is_outlier": False, "inverter_clipped": False},
        }
        if not await self._async_ready():
            return False
        try:
            observed_at_utc = (
                datetime.fromisoformat(baseline_time_local)
                .astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except ValueError:
            return False
        operation_sample_key = (
            f"{self._entry_id}:{interval_start_utc}:{interval_end_utc}"
        )
        operation_parameters = {
            "sample_key": operation_sample_key,
            "entry_id": self._entry_id,
            "target_date": date,
            "target_hour": hour,
            "actual_kwh": float(actual_kwh),
            "feature_snapshot": json.dumps(
                snapshot, sort_keys=True, separators=(",", ":")
            ),
            "accepted_at": snapshot["accepted_at"],
            "source": "metered_hourly_actual",
            "excluded": 0,
            "baseline_kwh": float(baseline_kwh),
        }
        operation_parameters.update(
            {
                "baseline_observed_at_utc": observed_at_utc,
                "baseline_local_date": baseline_date_local,
                "target_entity_id": source_entity_id,
                "target_unit": source_unit,
                "measurement_boundary_fingerprint": boundary_fingerprint,
                "updated_at_utc": observed_at_utc,
            }
        )
        try:
            await self._async_call_with_retry(
                lambda: self._service.execute_domain_operation(
                    self._lease,
                    "record_meter_interval",
                    operation_parameters,
                    idempotency_key=f"interval:{operation_sample_key}:{sensor_data['current_yield_kwh']}",
                )
            )
        except Exception as error:
            _LOGGER.warning(
                "Could not atomically persist EAI meter interval %s: %s",
                operation_sample_key,
                error,
            )
            return False
        return True

    async def async_load_samples(
        self, cutoff: str, before: str
    ) -> list[dict[str, Any]] | None:
        if not await self._async_ready():
            return
        try:
            rows = await self._async_call_with_retry(
                lambda: self._service.fetch_domain_operation(
                    self._lease,
                    "load_training_samples",
                    {
                        "entry_id": self._entry_id,
                        "cutoff": cutoff,
                        "before": before,
                        "excluded": 0,
                    },
                )
            )
            samples = []
            for row in rows:
                feature_snapshot = (
                    row.get("feature_snapshot") if isinstance(row, dict) else row[0]
                )
                snapshot = json.loads(feature_snapshot)
                if isinstance(snapshot, dict):
                    samples.append(snapshot)
            return samples
        except Exception as error:
            _LOGGER.warning("Could not load central EAI learning samples: %s", error)
            return
