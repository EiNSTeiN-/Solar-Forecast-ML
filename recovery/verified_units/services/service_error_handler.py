# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
Red alert error response system for Warp Core Simulation.
Provides containment breach circuit breaker pattern and centralized
antimatter cascade event logging. Uses TelemetryManager for error persistence.
"""
from __future__ import annotations
import asyncio
import logging
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional
from ..core.core_exceptions import (
    CircuitBreakerOpenException,
    ConfigurationException,
    DataIntegrityException,
    MLModelException,
    WeatherAPIException,
)
from ..data.db_manager import DatabaseManager

_LOGGER = logging.getLogger(__name__)


class CircuitBreakerState(Enum):
    """Possible states of the Circuit Breaker. @zara"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ErrorType(Enum):
    """Categorization of errors for circuit breaker and logging. @zara"""

    CONFIGURATION = "configuration"
    API_ERROR = "api_error"
    NETWORK_ERROR = "network_error"
    ML_TRAINING = "ml_training"
    ML_PREDICTION = "ml_prediction"
    DATA_INTEGRITY = "data_integrity"
    DATABASE_ERROR = "database_error"
    SENSOR_ERROR = "sensor_error"
    DEPENDENCY = "dependency"
    UNKNOWN = "unknown"


class CircuitBreaker:
    """Implements the Circuit Breaker pattern to prevent repeated failures. @zara"""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        success_threshold: int = 2,
        open_timeout_seconds: int = 60,
    ):
        """Initialize the Circuit Breaker. @zara"""
        if failure_threshold < 1 or success_threshold < 1 or open_timeout_seconds < 1:
            raise ValueError("Thresholds and timeout must be positive integers.")
        self.name = name
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.open_timeout = timedelta(seconds=open_timeout_seconds)
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self.opened_at_time = None
        self.last_state_change_time = datetime.now(timezone.utc)
        self.error_type_counts = defaultdict(int)
        _LOGGER.info(
            f"Circuit Breaker '{self.name}' initialized: FailureThreshold={failure_threshold}, SuccessThreshold={success_threshold}, OpenTimeout={open_timeout_seconds}s"
        )

    def _get_current_time(self) -> datetime:
        """Return the current time in UTC. @zara"""
        return datetime.now(timezone.utc)

    def _reset_counts(self) -> None:
        """Reset failure and success counts. @zara"""
        self.failure_count = 0
        self.success_count = 0

    def _change_state(self, new_state: CircuitBreakerState) -> None:
        """Handle state transitions and logging. @zara"""
        if self.state != new_state:
            previous_state = self.state
            self.state = new_state
            self.last_state_change_time = self._get_current_time()
            _LOGGER.info(
                f"Circuit Breaker '{self.name}' state changed: {previous_state.value} -> {new_state.value}"
            )
            self._reset_counts()
            if new_state == CircuitBreakerState.OPEN:
                self.opened_at_time = self.last_state_change_time
            else:
                self.opened_at_time = None
            if new_state == CircuitBreakerState.CLOSED:
                self.error_type_counts.clear()
        return None

    def allow_request(self) -> bool:
        """Check if the circuit breaker should allow the operation to proceed. @zara"""
        now = self._get_current_time()
        if self.state == CircuitBreakerState.CLOSED:
            return True
        if self.state == CircuitBreakerState.OPEN:
            if self.opened_at_time and now - self.opened_at_time >= self.open_timeout:
                self._change_state(CircuitBreakerState.HALF_OPEN)
                return True
            _LOGGER.debug(f"Circuit Breaker '{self.name}' is OPEN. Request blocked.")
            return False
        if self.state == CircuitBreakerState.HALF_OPEN:
            return True
        _LOGGER.error(f"Circuit Breaker '{self.name}' in unknown state: {self.state}")
        return False

    def record_success(self) -> None:
        """Record a successful operation. @zara"""
        if self.state == CircuitBreakerState.HALF_OPEN:
            self.success_count += 1
            _LOGGER.debug(
                f"Circuit Breaker '{self.name}' (HALF_OPEN): Success recorded ({self.success_count}/{self.success_threshold})."
            )
            if self.success_count >= self.success_threshold:
                self._change_state(CircuitBreakerState.CLOSED)
        return None

    def record_failure(self, error_type: ErrorType = ErrorType.UNKNOWN) -> None:
        """Record a failed operation. @zara"""
        now = self._get_current_time()
        self.last_failure_time = now
        self.error_type_counts[error_type] += 1
        _LOGGER.debug(
            f"Circuit Breaker '{self.name}': Failure recorded (Type: {error_type.value})."
        )
        if self.state == CircuitBreakerState.CLOSED:
            self.failure_count += 1
            _LOGGER.debug(
                f"Circuit Breaker '{self.name}' (CLOSED): Failure count incremented ({self.failure_count}/{self.failure_threshold})."
            )
            if self.failure_count >= self.failure_threshold:
                self._change_state(CircuitBreakerState.OPEN)
        elif self.state == CircuitBreakerState.HALF_OPEN:
            _LOGGER.warning(
                f"Circuit Breaker '{self.name}': Failure occurred in HALF_OPEN state. Re-opening circuit."
            )
            self._change_state(CircuitBreakerState.OPEN)
        if (
            error_type == ErrorType.CONFIGURATION
            and self.state != CircuitBreakerState.OPEN
        ):
            _LOGGER.warning(
                f"Circuit Breaker '{self.name}': Configuration error detected. Opening circuit immediately."
            )
            self._change_state(CircuitBreakerState.OPEN)
        return None

    def reset(self) -> None:
        """Manually reset the circuit breaker to the CLOSED state. @zara"""
        _LOGGER.info(f"Circuit Breaker '{self.name}' manually reset to CLOSED state.")
        self._change_state(CircuitBreakerState.CLOSED)
        self.last_failure_time = None

    def get_status(self) -> Dict[str, Any]:
        """Return the current status of the circuit breaker. @zara"""
        status = {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "failure_threshold": self.failure_threshold,
            "success_threshold": self.success_threshold,
            "open_timeout_seconds": self.open_timeout.total_seconds(),
            "error_types_count": {
                error_type.value: count
                for error_type, count in self.error_type_counts.items()
            },
            "last_failure_time": (
                self.last_failure_time.isoformat() if self.last_failure_time else None
            ),
            "last_state_change_time": self.last_state_change_time.isoformat(),
            "opened_at_time": (
                self.opened_at_time.isoformat() if self.opened_at_time else None
            ),
        }
        if self.state == CircuitBreakerState.OPEN and self.opened_at_time:
            remaining_time = self.open_timeout - (
                self._get_current_time() - self.opened_at_time
            )
            status["open_time_remaining_seconds"] = max(
                0, round(remaining_time.total_seconds())
            )
        else:
            status["open_time_remaining_seconds"] = None
        return status if True else None


class ErrorHandlingService:
    """Central service for handling errors and logging operational details. @zara"""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        """Initialize the Error Handling Service. @zara"""
        self._db = db_manager
        self.circuit_breakers = {}
        self.error_log = []
        self.ml_operation_log = []
        self.db_operation_log = []
        self.sensor_status_log = []
        self.max_error_log_size = 100
        self.max_ml_log_size = 200
        self.max_db_log_size = 100
        self.max_sensor_log_size = 50
        _LOGGER.debug("ErrorHandlingService initialized.")

    def set_db_manager(self, db_manager: DatabaseManager) -> None:
        """Set database manager after initialization. @zara"""
        self._db = db_manager

    def register_circuit_breaker(
        self,
        name: str,
        failure_threshold: int = 3,
        success_threshold: int = 2,
        open_timeout_seconds: int = 60,
    ) -> CircuitBreaker:
        """Register and configure a new circuit breaker. @zara"""
        if name in self.circuit_breakers:
            _LOGGER.warning(
                f"Circuit Breaker '{name}' is already registered. Returning existing instance."
            )
            return self.circuit_breakers[name]
        try:
            breaker = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                success_threshold=success_threshold,
                open_timeout_seconds=open_timeout_seconds,
            )
            self.circuit_breakers[name] = breaker
            _LOGGER.info(f"Circuit Breaker '{name}' registered successfully.")
            return breaker
        except ValueError as error:
            _LOGGER.error(f"Failed to register Circuit Breaker '{name}': {error}")
            raise

    def get_circuit_breaker(self, name: str) -> Optional[CircuitBreaker]:
        """Get a registered circuit breaker by name. @zara"""
        breaker = self.circuit_breakers.get(name)
        if breaker is None:
            _LOGGER.warning(f"Attempted to get non-existent Circuit Breaker '{name}'.")
        return breaker

    async def execute_with_circuit_breaker(
        self,
        breaker_name: str,
        operation: Callable[..., Coroutine[Any, Any, Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute an asynchronous operation protected by a circuit breaker. @zara"""
        breaker = self.get_circuit_breaker(breaker_name)
        if breaker is None:
            raise ValueError(f"Circuit Breaker '{breaker_name}' is not registered.")
        if not breaker.allow_request():
            message = f"Circuit Breaker '{breaker_name}' is {breaker.state.value}. Operation blocked."
            _LOGGER.warning(message)
            self._log_error(
                breaker_name,
                CircuitBreakerOpenException.__name__,
                message,
                ErrorType.UNKNOWN,
            )
            raise CircuitBreakerOpenException(message)
        try:
            result = await operation(*args, **kwargs)
            breaker.record_success()
            _LOGGER.debug(
                f"Operation '{operation.__name__}' executed successfully via Circuit Breaker '{breaker_name}'."
            )
            return result
        except Exception as error:
            classification = self._classify_error(error)
            _LOGGER.error(
                f"Operation '{operation.__name__}' failed via Circuit Breaker '{breaker_name}': {error}",
                exc_info=False,
            )
            breaker.record_failure(classification)
            await self.handle_error(
                error,
                source=f"circuit_breaker_{breaker_name}",
                context={"operation": operation.__name__},
            )
            raise

    async def handle_error(
        self,
        error: Exception,
        source: str,
        context: Optional[Dict[str, Any]] = None,
        pipeline_position: Optional[str] = None,
    ) -> None:
        """Log and store detailed information about an encountered error. @zara"""
        classification = self._classify_error(error)
        error_name = type(error).__name__
        error_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "error_type": error_name,
            "error_classification": classification.value,
            "message": str(error),
            "pipeline_position": pipeline_position,
            "context": context or {},
            "stack_trace": (
                traceback.format_exc()
                if isinstance(error, (MLModelException, DataIntegrityException))
                else None
            ),
        }
        self.error_log.append(error_entry)
        if len(self.error_log) > self.max_error_log_size:
            self.error_log = self.error_log[-self.max_error_log_size :]
        if self._db:
            try:
                await self._db.execute(
                    "INSERT INTO error_log\n                       (timestamp, source, error_type, classification, message, context)\n                       VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        error_entry["timestamp"],
                        source,
                        error_name,
                        classification.value,
                        str(error),
                        str(context) if context else None,
                    ),
                )
            except Exception as persistence_error:
                _LOGGER.debug(f"Could not persist error to DB: {persistence_error}")
        log_level = (
            logging.ERROR
            if isinstance(
                error,
                (MLModelException, DataIntegrityException, ConfigurationException),
            )
            else logging.WARNING
        )
        _LOGGER.log(
            log_level,
            f"[ERROR] Source: {source} | Type: {error_name} ({classification.value}) | Position: {pipeline_position or 'N/A'} | Message: {error}",
            exc_info=error_entry["stack_trace"] is not None,
        )
        if context:
            _LOGGER.debug(f"  Error Context: {context}")
        return None

    def _classify_error(self, error: Exception) -> ErrorType:
        """Classify an exception into an ErrorType category. @zara"""
        if isinstance(error, MLModelException):
            model_message = str(error).lower()
            if "training" in model_message:
                return ErrorType.ML_TRAINING
            if "prediction" in model_message:
                return ErrorType.ML_PREDICTION
            return ErrorType.ML_PREDICTION
        if isinstance(error, DataIntegrityException):
            return ErrorType.DATA_INTEGRITY
        if isinstance(error, ConfigurationException):
            return ErrorType.CONFIGURATION
        if isinstance(error, WeatherAPIException):
            return ErrorType.API_ERROR
        if isinstance(error, CircuitBreakerOpenException):
            return ErrorType.UNKNOWN
        if isinstance(error, asyncio.TimeoutError):
            return ErrorType.NETWORK_ERROR
        if isinstance(error, OSError):
            if "Network is unreachable" in str(error) or "Connection refused" in str(
                error
            ):
                return ErrorType.NETWORK_ERROR
            return ErrorType.DATABASE_ERROR
        if isinstance(error, ImportError):
            return ErrorType.DEPENDENCY
        message_text = str(error).lower()
        if (
            "network" in message_text
            or "connection" in message_text
            or "timeout" in message_text
        ):
            return ErrorType.NETWORK_ERROR
        if (
            "sensor" in message_text
            or "state" in message_text
            or "entity not found" in message_text
        ):
            return ErrorType.SENSOR_ERROR
        if (
            "database" in message_text
            or "sqlite" in message_text
            or "sql" in message_text
        ):
            return ErrorType.DATABASE_ERROR
        return ErrorType.UNKNOWN

    def log_ml_operation(
        self,
        operation: str,
        success: bool,
        metrics: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        duration_seconds: Optional[float] = None,
    ) -> None:
        """Log the outcome of a Machine Learning operation. @zara"""
        timestamp = datetime.now(timezone.utc).isoformat()
        entry = {
            "timestamp": timestamp,
            "operation": operation,
            "success": success,
            "metrics": metrics or {},
            "context": context or {},
            "duration_seconds": duration_seconds,
        }
        self.ml_operation_log.append(entry)
        if len(self.ml_operation_log) > self.max_ml_log_size:
            self.ml_operation_log = self.ml_operation_log[-self.max_ml_log_size :]
        status_text = "Success" if success else "FAILED"
        duration_text = (
            f"{duration_seconds:.2f}s" if duration_seconds is not None else "N/A"
        )
        metrics_text = f"Metrics: {metrics}" if metrics else ""
        log_level = logging.INFO if success else logging.ERROR
        _LOGGER.log(
            log_level,
            f"[ML OP] {operation}: {status_text} | Duration: {duration_text} | {metrics_text}",
        )
        if context:
            _LOGGER.debug(f"  ML Op Context: {context}")
        return None

    def log_db_operation(
        self,
        table_name: str,
        operation: str,
        success: bool,
        records_count: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Log the outcome of a database operation. @zara"""
        timestamp = datetime.now(timezone.utc).isoformat()
        entry = {
            "timestamp": timestamp,
            "table_name": table_name,
            "operation": operation,
            "success": success,
            "records_count": records_count,
            "error_message": error_message,
        }
        self.db_operation_log.append(entry)
        if len(self.db_operation_log) > self.max_db_log_size:
            self.db_operation_log = self.db_operation_log[-self.max_db_log_size :]
        status_text = "Success" if success else "FAILED"
        log_level = logging.INFO if success else logging.ERROR
        detail = ""
        if success:
            records_text = (
                f"{records_count} records" if records_count is not None else ""
            )
            detail = f"| {records_text}".strip()
        else:
            detail = f"| Error: {error_message or 'Unknown'}"
        _LOGGER.log(
            log_level, f"[DB OP] {operation} on {table_name}: {status_text} {detail}"
        )

    def log_sensor_status(
        self,
        sensor_name: str,
        sensor_type: str,
        available: bool,
        value: Optional[Any] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Log the status and value of critical external sensors. @zara"""
        timestamp = datetime.now(timezone.utc).isoformat()
        entry = {
            "timestamp": timestamp,
            "sensor_name": sensor_name,
            "sensor_type": sensor_type,
            "available": available,
            "value": str(value) if value is not None else None,
            "error_message": error_message,
        }
        self.sensor_status_log.append(entry)
        if len(self.sensor_status_log) > self.max_sensor_log_size:
            self.sensor_status_log = self.sensor_status_log[-self.max_sensor_log_size :]
        status_text = "Available" if available else "UNAVAILABLE"
        if available:
            _LOGGER.debug(
                f"[SENSOR] {sensor_name} ({sensor_type}): {status_text} | Value: {value}"
            )
        else:
            _LOGGER.warning(
                f"[SENSOR] {sensor_name} ({sensor_type}): {status_text} | Error: {error_message or 'Unknown'}"
            )
        return None if True else None

    def _log_error(
        self,
        source: str,
        error_type_name: str,
        message: str,
        error_classification: ErrorType = ErrorType.UNKNOWN,
    ) -> None:
        """Internal helper to add simple errors to the main error log. @zara"""
        timestamp = datetime.now(timezone.utc).isoformat()
        error_record = {
            "timestamp": timestamp,
            "source": source,
            "error_type": error_type_name,
            "message": message,
            "classification": error_classification.value,
        }
        self.error_log.append(error_record)
        if len(self.error_log) > self.max_error_log_size:
            self.error_log = self.error_log[-self.max_error_log_size :]
        return None

    def get_error_log(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return the most recent error log entries. @zara"""
        return self.error_log[-limit:]

    def get_ml_operation_log(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return the most recent ML operation log entries. @zara"""
        return self.ml_operation_log[-limit:]

    def get_db_operation_log(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return the most recent database operation log entries. @zara"""
        return self.db_operation_log[-limit:]

    def get_sensor_status_log(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return the most recent sensor status log entries. @zara"""
        return self.sensor_status_log[-limit:]

    def clear_error_log(self) -> None:
        """Clear all entries from the error log. @zara"""
        self.error_log.clear()
        _LOGGER.info("Error log cleared.")

    def clear_ml_operation_log(self) -> None:
        """Clear all entries from the ML operation log. @zara"""
        self.ml_operation_log.clear()
        _LOGGER.info("ML operation log cleared.")

    def clear_db_operation_log(self) -> None:
        """Clear all entries from the database operation log. @zara"""
        self.db_operation_log.clear()
        _LOGGER.info("Database operation log cleared.")

    def clear_sensor_status_log(self) -> None:
        """Clear all entries from the sensor status log. @zara"""
        self.sensor_status_log.clear()
        _LOGGER.info("Sensor status log cleared.")

    def get_all_status(self) -> Dict[str, Any]:
        """Return a summary of the error handler's status and recent logs. @zara"""
        breaker_statuses = {
            name: breaker.get_status()
            for name, breaker in self.circuit_breakers.items()
        }
        return {
            "circuit_breakers": breaker_statuses,
            "log_sizes": {
                "error": len(self.error_log),
                "ml_operation": len(self.ml_operation_log),
                "db_operation": len(self.db_operation_log),
                "sensor_status": len(self.sensor_status_log),
            },
            "recent_errors": self.get_error_log(5),
            "recent_ml_operations": self.get_ml_operation_log(5),
            "recent_db_operations": self.get_db_operation_log(5),
            "recent_sensor_status": self.get_sensor_status_log(5),
        }

    def reset_all_circuit_breakers(self) -> None:
        """Manually reset all registered circuit breakers to the CLOSED state. @zara"""
        _LOGGER.info("Resetting all registered circuit breakers...")
        reset_count = 0
        for name, breaker in self.circuit_breakers.items():
            breaker.reset()
            reset_count += 1
        _LOGGER.info(f"Reset {reset_count} circuit breaker(s).")

    async def get_error_history_from_db(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get error history from database. @zara"""
        if not self._db:
            return self.error_log[-limit:]
        try:
            rows = await self._db.fetchall(
                "SELECT timestamp, source, error_type, classification, message\n                   FROM error_log\n                   ORDER BY timestamp DESC\n                   LIMIT ?",
                (limit,),
            )
            return [
                {
                    "timestamp": row[0],
                    "source": row[1],
                    "error_type": row[2],
                    "classification": row[3],
                    "message": row[4],
                }
                for row in rows
            ]
        except Exception as error:
            _LOGGER.warning(f"Could not fetch error history from DB: {error}")
            return self.error_log[-limit:]
