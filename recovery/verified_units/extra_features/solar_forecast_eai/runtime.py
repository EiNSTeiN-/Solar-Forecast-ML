# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Licensed lifecycle boundary for the EAI coordinator and AI stack."""
from __future__ import annotations
import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING, Any

try:
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    from homeassistant.core import callback
except ImportError:
    EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"

    def callback(func: Any) -> Any:
        return func


from .const import (
    CONF_HEAT_PUMP_ENABLED,
    CONF_LICENSE_KEY,
    CONF_WALLBOX_ENABLED,
    CONF_WEATHER_FUSION_ENTRY_ID,
    CONF_WEATHER_HISTORY_DAYS,
    CONF_WEATHER_INTELLIGENCE_ENABLED,
    DEFAULT_WEATHER_HISTORY_DAYS,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
_LOGGER = logging.getLogger(__name__)
_WEATHER_STARTUP_INTERVAL_SECONDS = 15
_WEATHER_STEADY_INTERVAL_SECONDS = 60
_WEATHER_HISTORY_RETRY_SECONDS = 60
_WEATHER_HISTORY_REFRESH_SECONDS = 900
_WEATHER_UPDATE_TIMEOUT_SECONDS = 90
_WEATHER_EVENT_DEBOUNCE_SECONDS = 2
_WEATHER_TASK_RESTART_SECONDS = 5
_SENSOR_RECONCILIATION_SECONDS = 30


class EAIRuntime:
    """Own every EAI background resource associated with one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        provider: Any,
        *,
        dependency_factory: Callable[[], Any] | None = None,
        coordinator_factory: Callable[..., Any] | None = None,
        license_validator: Any | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.provider = provider
        self._dependency_factory = dependency_factory
        self._coordinator_factory = coordinator_factory
        self._license_validator = license_validator
        self.coordinator = None
        self._first_refresh_task = None
        self._weather_task = None
        self._weather_restart_task = None
        self._weather_event_task = None
        self._weather_update_lock = asyncio.Lock()
        self._weather_state_unsub = None
        self._weather_debounce_unsub = None
        self._weather_entity_ids = ()
        self._weather_history_last_attempt = 0.0
        self._license_task = None
        self._insights_save_task = None
        self._insights_save_dirty = False
        self._insights_learning_task = None
        self._sensor_reconciliation_task = None
        self._insights_unsub = None
        self._insights_store = None
        self._weather_engine = None
        self._weather_intelligence_listeners = []
        self._homeassistant_stop_unsub = None
        self._shutdown_finalizer_task = None
        self._shutdown_finalized = False
        self.weather_intelligence_snapshot = {
            "available": False,
            "status": "disabled",
            "source_status": "disabled",
            "data_quality": {"score_percent": 0, "paired_samples": 0},
            "accuracy": {},
            "uncertainty": {"available": False, "reason": "disabled"},
            "events": [],
            "cold_start": True,
        }
        self._status = "initializing"
        self._failure_code = None
        self._dependencies_ok = False
        self._stopping = False
        from .automation import EAIRecommendationEngine as recommendation_engine_type

        self.recommendation_engine = recommendation_engine_type(self)

    def schedule_license_monitor(self, expires_at: datetime | None) -> None:
        """Revalidate expiring licenses while Home Assistant keeps running."""
        if expires_at is None or self._license_validator is None:
            return None
        self._license_task = self.hass.async_create_task(
            self._async_monitor_license(expires_at),
            name=f"solar_forecast_eai_license_{self.entry.entry_id}",
        )

    async def _async_monitor_license(self, expires_at: datetime) -> None:
        """Reload the entry as soon as its signed entitlement is no longer valid."""
        retry_delay_seconds = 60.0
        while not self._stopping:
            now_utc = datetime.now(timezone.utc)
            expiration_utc = (
                expires_at.astimezone(timezone.utc)
                if expires_at.tzinfo
                else expires_at.replace(tzinfo=timezone.utc)
            )
            await asyncio.sleep(
                max(0.0, min(3600.0, (expiration_utc - now_utc).total_seconds()))
            )
            if self._stopping:
                return
            try:
                license_result = self._license_validator.validate(
                    getattr(self.entry, "data", {}).get(CONF_LICENSE_KEY, "")
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                fail_license_check = getattr(self.provider, "fail_license_check", None)
                if callable(fail_license_check):
                    fail_license_check()
                _LOGGER.exception(
                    "EAI license revalidation failed; retrying in %.0f seconds",
                    retry_delay_seconds,
                )
                await asyncio.sleep(retry_delay_seconds)
                retry_delay_seconds = min(retry_delay_seconds * 2, 3600.0)
                continue
            retry_delay_seconds = 60.0
            self.provider.update_license(license_result)
            if license_result.status.value != "valid":
                self._status = "license_invalid"
                self._failure_code = license_result.status.value
                self.entry.async_start_reauth(self.hass)
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(self.entry.entry_id),
                    name=f"solar_forecast_eai_license_reload_{self.entry.entry_id}",
                )
                return
            if (
                license_result.payload is None
                or license_result.payload.expires_at is None
            ):
                return None
            expires_at = license_result.payload.expires_at

    async def async_setup(self) -> bool:
        """Start the coordinator only after the caller validated the license."""
        self._register_homeassistant_stop_listener()
        self.provider.attach_runtime(self)
        try:
            await self._async_restore_insights_learning()
            await self._async_setup_weather_intelligence()
            merged_config = {
                **getattr(self.entry, "data", {}),
                **getattr(self.entry, "options", {}),
            }
            if not (
                merged_config.get(CONF_HEAT_PUMP_ENABLED, True)
                or merged_config.get(CONF_WALLBOX_ENABLED, False)
            ):
                self._dependencies_ok = True
                self._status = "active"
                return True
            dependency_factory = self._dependency_factory
            if dependency_factory is None:
                from .core.core_dependency_handler import (
                    DependencyHandler as dependency_handler_type,
                )

                dependency_factory = dependency_handler_type
            dependency_handler = dependency_factory()
            self._dependencies_ok = await dependency_handler.check_dependencies(
                self.hass
            )
            if not self._dependencies_ok:
                self._status = "degraded"
                self._failure_code = "dependencies_missing"
                return False
            coordinator_factory = self._coordinator_factory
            if coordinator_factory is None:
                from .coordinator import SolarForecastEAICoordinator as coordinator_type

                coordinator_factory = coordinator_type
            coordinator = coordinator_factory(
                self.hass, self.entry, dependencies_ok=self._dependencies_ok
            )
            self.coordinator = coordinator
            if not await coordinator.async_setup():
                self._status = "degraded"
                self._failure_code = "coordinator_setup_failed"
                await self._async_shutdown_coordinator()
                return False
            self._status = "active"
            self.recommendation_engine.connect(coordinator)
            self._sensor_reconciliation_task = self.hass.async_create_background_task(
                self._async_sensor_reconciliation_loop(),
                name=f"solar_forecast_eai_sensor_reconciliation_{self.entry.entry_id}",
            )
            add_coordinator_listener = getattr(coordinator, "async_add_listener", None)
            if callable(add_coordinator_listener):
                self._insights_unsub = add_coordinator_listener(
                    self._capture_and_persist_insights_learning
                )
            self._capture_and_persist_insights_learning()
            if callable(getattr(self.provider, "capture_learning_sample", None)):
                self._insights_learning_task = self.hass.async_create_background_task(
                    self._async_insights_learning_loop(),
                    name=f"solar_forecast_eai_insights_sampler_{self.entry.entry_id}",
                )
            self._first_refresh_task = self.hass.async_create_task(
                self._async_first_refresh(),
                name=f"solar_forecast_eai_first_refresh_{self.entry.entry_id}",
            )
        except asyncio.CancelledError:
            raise
        except Exception as runtime_error:
            self._status = "degraded"
            self._failure_code = type(runtime_error).__name__
            _LOGGER.exception("EAI coordinator startup failed")
            await self._async_shutdown_coordinator()
            return False
        return True

    async def _async_sensor_reconciliation_loop(self) -> None:
        """Keep configured-source listeners healthy without delaying live events."""
        while not self._stopping:
            try:
                await asyncio.sleep(_SENSOR_RECONCILIATION_SECONDS)
                if self._stopping:
                    return
                self.recommendation_engine.reconcile_sources()
                self.recommendation_engine.invalidate(force_notify=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("EAI sensor reconciliation iteration failed")
                continue

    async def _async_restore_insights_learning(self) -> None:
        try:
            from homeassistant.helpers.storage import Store as storage_type
        except ImportError:
            return
        self._insights_store = storage_type(
            self.hass, 1, f"{DOMAIN}.insights_learning.{self.entry.entry_id}"
        )
        try:
            stored_learning = await self._insights_store.async_load()
        except Exception:
            _LOGGER.warning("EAI building-learning history could not be loaded")
            return
        restore_samples = getattr(self.provider, "restore_learning_samples", None)
        if callable(restore_samples):
            learning_samples = (
                stored_learning.get("samples", [])
                if isinstance(stored_learning, dict)
                and stored_learning.get("schema_version") == 1
                else []
            )
            restore_samples(learning_samples)
        restore_daily_energy = getattr(
            self.provider, "restore_daily_energy_state", None
        )
        if callable(restore_daily_energy) and isinstance(stored_learning, dict):
            restore_daily_energy(stored_learning.get("daily_energy"))
        return None if True else None

    def _capture_and_persist_insights_learning(self) -> None:
        capture_sample = getattr(self.provider, "capture_learning_sample", None)
        if not callable(capture_sample):
            return
        try:
            captured_sample = capture_sample()
        except Exception:
            _LOGGER.exception("EAI building-learning sample capture failed")
            return
        if not captured_sample and (
            not getattr(self.provider, "daily_energy_dirty", False)
        ):
            return
        self._insights_save_dirty = True
        if self._insights_save_task is not None and (
            not self._insights_save_task.done()
        ):
            return
        self._insights_save_task = self.hass.async_create_task(
            self._async_save_insights_learning(),
            name=f"solar_forecast_eai_insights_learning_{self.entry.entry_id}",
        )

    async def _async_save_insights_learning(self) -> None:
        if self._insights_store is None:
            self._insights_save_dirty = False
            return
        while self._insights_save_dirty:
            self._insights_save_dirty = False
            try:
                export_samples = getattr(self.provider, "export_learning_samples", None)
                learning_samples = export_samples() if callable(export_samples) else []
                export_daily_energy = getattr(
                    self.provider, "export_daily_energy_state", None
                )
                await self._insights_store.async_save(
                    {
                        "schema_version": 1,
                        "samples": learning_samples,
                        "daily_energy": (
                            export_daily_energy()
                            if callable(export_daily_energy)
                            else None
                        ),
                    }
                )
            except Exception:
                _LOGGER.exception("EAI building-learning history could not be saved")
                return

    async def _async_insights_learning_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(300)
                if not self._stopping:
                    self._capture_and_persist_insights_learning()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("EAI building-learning sampler iteration failed")
                continue

    async def _async_setup_weather_intelligence(self) -> None:
        merged_config = {
            **getattr(self.entry, "data", {}),
            **getattr(self.entry, "options", {}),
        }
        if not merged_config.get(CONF_WEATHER_INTELLIGENCE_ENABLED, False):
            return
        from homeassistant.helpers.storage import Store as storage_type
        from .weather_forecast_store import ForecastSnapshotStore as forecast_store_type
        from .weather_intelligence import (
            WeatherIntelligenceEngine as weather_engine_type,
        )

        history_days = max(
            7,
            min(
                365,
                int(
                    merged_config.get(
                        CONF_WEATHER_HISTORY_DAYS, DEFAULT_WEATHER_HISTORY_DAYS
                    )
                ),
            ),
        )
        weather_storage = storage_type(
            self.hass, 1, f"{DOMAIN}.weather_intelligence.{self.entry.entry_id}"
        )
        forecast_store = forecast_store_type(
            weather_storage,
            pair_retention=timedelta(days=history_days),
            max_pairs=history_days * 24 * 3,
        )
        self._weather_engine = weather_engine_type(
            self.hass,
            merged_config.get(CONF_WEATHER_FUSION_ENTRY_ID),
            sfml_entry_id=merged_config.get("sfml_entry_id"),
            store=forecast_store,
            history_days=history_days,
        )
        await self._async_update_weather_intelligence(refresh_history=False)
        self._start_weather_loop()

    def _start_weather_loop(self) -> None:
        if self._stopping:
            return
        if self._weather_task is not None and (not self._weather_task.done()):
            return
        weather_task = self.hass.async_create_background_task(
            self._async_weather_loop(),
            name=f"solar_forecast_eai_weather_intelligence_{self.entry.entry_id}",
        )
        self._weather_task = weather_task
        weather_task.add_done_callback(self._weather_task_finished)

    def _register_homeassistant_stop_listener(self) -> None:
        """Mark this runtime stopped before Home Assistant cancels its tasks."""
        if self._homeassistant_stop_unsub is not None:
            return
        event_bus = getattr(self.hass, "bus", None)
        listen_once = getattr(event_bus, "async_listen_once", None)
        if callable(listen_once):
            self._homeassistant_stop_unsub = listen_once(
                EVENT_HOMEASSISTANT_STOP, self._handle_homeassistant_stop
            )
        return None if True else None

    @callback
    def _handle_homeassistant_stop(self, _event: Any) -> None:
        """Suppress task supervision while Home Assistant is shutting down."""
        self._homeassistant_stop_unsub = None
        self._quiesce_shutdown(cancel_insights_save=True)
        self._schedule_shutdown_finalizer()

    def _quiesce_shutdown(self, *, cancel_insights_save: bool) -> bool:
        """Mark shutdown once and synchronously cancel background work."""
        if self._stopping:
            return False
        self._stopping = True
        self._status = "stopping"
        if self._homeassistant_stop_unsub is not None:
            self._homeassistant_stop_unsub()
            self._homeassistant_stop_unsub = None
        if self._insights_unsub is not None:
            self._insights_unsub()
            self._insights_unsub = None
        if self._weather_state_unsub is not None:
            self._weather_state_unsub()
            self._weather_state_unsub = None
        self._weather_entity_ids = ()
        if self._weather_debounce_unsub is not None:
            self._weather_debounce_unsub()
            self._weather_debounce_unsub = None
        owned_tasks = (
            self._first_refresh_task,
            self._weather_event_task,
            self._weather_restart_task,
            self._weather_task,
            self._license_task,
            self._insights_learning_task,
            self._sensor_reconciliation_task,
        )
        if cancel_insights_save:
            owned_tasks += (self._insights_save_task,)
        for task in owned_tasks:
            if task is not None and (not task.done()):
                task.cancel()
        return True

    def _schedule_shutdown_finalizer(self) -> asyncio.Task[Any]:
        """Create the one finalizer shared by stop events and entry unload."""
        finalizer_task = self._shutdown_finalizer_task
        if finalizer_task is None:
            finalizer_task = self.hass.async_create_task(
                self._async_finalize_shutdown(),
                name=f"solar_forecast_eai_shutdown_{self.entry.entry_id}",
            )
            self._shutdown_finalizer_task = finalizer_task
        return finalizer_task

    def _weather_task_finished(self, task: asyncio.Task[Any]) -> None:
        if task is not self._weather_task:
            return
        self._weather_task = None
        if self._stopping:
            return
        try:
            task_error = task.exception()
        except asyncio.CancelledError:
            task_error = None
        if task_error is None:
            _LOGGER.error("EAI weather intelligence loop stopped unexpectedly")
        else:
            _LOGGER.error(
                "EAI weather intelligence loop failed unexpectedly",
                exc_info=(type(task_error), task_error, task_error.__traceback__),
            )
        if self._weather_restart_task is None or self._weather_restart_task.done():
            self._weather_restart_task = self.hass.async_create_task(
                self._async_restart_weather_loop(),
                name=f"solar_forecast_eai_weather_restart_{self.entry.entry_id}",
            )
        return None if True else None

    async def _async_restart_weather_loop(self) -> None:
        try:
            await asyncio.sleep(_WEATHER_TASK_RESTART_SECONDS)
            self._start_weather_loop()
        except asyncio.CancelledError:
            raise

    def _weather_history_refresh_required(self) -> bool:
        elapsed_seconds = monotonic() - self._weather_history_last_attempt
        actual_history = self.weather_intelligence_snapshot.get("actual_history", {})
        refresh_interval_seconds = (
            _WEATHER_HISTORY_REFRESH_SECONDS
            if actual_history.get("available")
            else _WEATHER_HISTORY_RETRY_SECONDS
        )
        return (
            self._weather_history_last_attempt == 0.0
            or elapsed_seconds >= refresh_interval_seconds
        )

    async def _async_update_weather_intelligence(
        self, *, refresh_history: bool | None = None
    ) -> None:
        if self._weather_engine is None:
            return
        async with self._weather_update_lock:
            if refresh_history is None:
                refresh_history = self._weather_history_refresh_required()
            if refresh_history:
                self._weather_history_last_attempt = monotonic()
            try:
                async with asyncio.timeout(_WEATHER_UPDATE_TIMEOUT_SECONDS):
                    self.weather_intelligence_snapshot = (
                        await self._weather_engine.async_snapshot(
                            refresh_history=refresh_history
                        )
                    )
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "EAI weather intelligence update timed out after %d seconds",
                    _WEATHER_UPDATE_TIMEOUT_SECONDS,
                )
                self.weather_intelligence_snapshot = {
                    **self.weather_intelligence_snapshot,
                    "available": False,
                    "status": "degraded",
                    "source_status": "engine_timeout",
                    "error": "TimeoutError",
                }
            except asyncio.CancelledError:
                raise
            except Exception as runtime_error:
                _LOGGER.exception("EAI weather intelligence update failed")
                self.weather_intelligence_snapshot = {
                    **self.weather_intelligence_snapshot,
                    "available": False,
                    "status": "degraded",
                    "source_status": "engine_error",
                    "error": type(runtime_error).__name__,
                }
            self._sync_weather_state_listener()
        for listener in tuple(self._weather_intelligence_listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception("EAI weather intelligence listener refresh failed")
                continue
        return None if True else None

    def _sync_weather_state_listener(self) -> None:
        get_station_entity_ids = getattr(
            self._weather_engine, "station_entity_ids", None
        )
        if not callable(get_station_entity_ids):
            return
        try:
            station_entity_ids = tuple(get_station_entity_ids())
        except Exception:
            _LOGGER.exception("EAI weather station subscription discovery failed")
            return
        if station_entity_ids == self._weather_entity_ids:
            return
        if self._weather_state_unsub is not None:
            try:
                self._weather_state_unsub()
            except Exception:
                _LOGGER.exception("EAI weather station listener cleanup failed")
            self._weather_state_unsub = None
        self._weather_entity_ids = station_entity_ids
        if not station_entity_ids:
            return
        try:
            from homeassistant.helpers.event import (
                async_track_state_change_event as track_state_change_event,
            )

            self._weather_state_unsub = track_state_change_event(
                self.hass, station_entity_ids, self._weather_station_state_changed
            )
        except Exception:
            _LOGGER.exception("EAI weather station listener registration failed")
            self._weather_entity_ids = ()
        return None if True else None

    def _defer_to_hass_loop(self, target: Callable[[Any], None], argument: Any) -> bool:
        hass_loop = getattr(self.hass, "loop", None)
        if hass_loop is None:
            return False
        try:
            if asyncio.get_running_loop() is hass_loop:
                return False
        except RuntimeError:
            pass
        hass_loop.call_soon_threadsafe(target, argument)
        return True

    @callback
    def _weather_station_state_changed(self, _event: Any) -> None:
        if self._defer_to_hass_loop(self._weather_station_state_changed, _event):
            return
        from homeassistant.helpers.event import async_call_later as call_later

        if self._weather_debounce_unsub is not None:
            self._weather_debounce_unsub()
        self._weather_debounce_unsub = call_later(
            self.hass,
            _WEATHER_EVENT_DEBOUNCE_SECONDS,
            self._schedule_weather_event_refresh,
        )

    @callback
    def _schedule_weather_event_refresh(self, _now: Any) -> None:
        if self._defer_to_hass_loop(self._schedule_weather_event_refresh, _now):
            return
        self._weather_debounce_unsub = None
        if self._stopping:
            return
        if self._weather_event_task is not None and (
            not self._weather_event_task.done()
        ):
            return
        self._weather_event_task = self.hass.async_create_task(
            self._async_update_weather_intelligence(refresh_history=False),
            name=f"solar_forecast_eai_weather_event_{self.entry.entry_id}",
        )

    def add_weather_intelligence_listener(
        self, listener: Callable[[], None]
    ) -> Callable[[], None]:
        """Register an entity refresh callback for weather intelligence updates."""
        self._weather_intelligence_listeners.append(listener)

        def remove_listener() -> None:
            if listener in self._weather_intelligence_listeners:
                self._weather_intelligence_listeners.remove(listener)
            return None if True else None

        return remove_listener

    async def _async_weather_loop(self) -> None:
        interval_seconds = _WEATHER_STARTUP_INTERVAL_SECONDS
        while not self._stopping:
            try:
                await asyncio.sleep(interval_seconds)
                if not self._stopping:
                    await self._async_update_weather_intelligence()
                    interval_seconds = (
                        _WEATHER_STEADY_INTERVAL_SECONDS
                        if self._weather_snapshot_ready()
                        else _WEATHER_STARTUP_INTERVAL_SECONDS
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "EAI weather intelligence recovery loop iteration failed"
                )
                interval_seconds = _WEATHER_STARTUP_INTERVAL_SECONDS
                continue

    def _weather_snapshot_ready(self) -> bool:
        weather_snapshot = self.weather_intelligence_snapshot
        return bool(
            weather_snapshot.get("available")
            and weather_snapshot.get("data_quality", {}).get("actual_available")
            and weather_snapshot.get("actual_history", {}).get("available")
        )

    async def _async_first_refresh(self) -> None:
        try:
            await asyncio.sleep(5)
            if self._stopping or self.coordinator is None:
                return None
            async with asyncio.timeout(60):
                await self.coordinator.async_refresh()
            await self._async_update_weather_intelligence(refresh_history=False)
        except asyncio.TimeoutError:
            _LOGGER.info("EAI first refresh deferred after startup timeout")
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "EAI first refresh failed; scheduled updates remain active"
            )
        return None if True else None

    async def async_shutdown(self) -> None:
        """Cancel owned work and close the coordinator idempotently."""
        self._quiesce_shutdown(cancel_insights_save=False)
        finalizer_task = self._schedule_shutdown_finalizer()
        if finalizer_task is asyncio.current_task():
            return
        await asyncio.shield(finalizer_task)

    async def _async_finalize_shutdown(self) -> None:
        """Await quiesced work and detach entry resources exactly once."""
        if self._shutdown_finalized:
            return
        first_refresh_task = self._first_refresh_task
        self._first_refresh_task = None
        if first_refresh_task is not None and (not first_refresh_task.done()):
            first_refresh_task.cancel()
            try:
                await first_refresh_task
            except asyncio.CancelledError:
                pass
        weather_event_task = self._weather_event_task
        self._weather_event_task = None
        if weather_event_task is not None and (not weather_event_task.done()):
            weather_event_task.cancel()
            try:
                await weather_event_task
            except asyncio.CancelledError:
                pass
        weather_restart_task = self._weather_restart_task
        self._weather_restart_task = None
        if weather_restart_task is not None and (not weather_restart_task.done()):
            weather_restart_task.cancel()
            try:
                await weather_restart_task
            except asyncio.CancelledError:
                pass
        weather_task = self._weather_task
        self._weather_task = None
        if weather_task is not None and (not weather_task.done()):
            weather_task.cancel()
            try:
                await weather_task
            except asyncio.CancelledError:
                pass
        license_task = self._license_task
        self._license_task = None
        if license_task is not None and (not license_task.done()):
            license_task.cancel()
            try:
                await license_task
            except asyncio.CancelledError:
                pass
        insights_learning_task = self._insights_learning_task
        self._insights_learning_task = None
        if insights_learning_task is not None and (not insights_learning_task.done()):
            insights_learning_task.cancel()
            try:
                await insights_learning_task
            except asyncio.CancelledError:
                pass
        sensor_reconciliation_task = self._sensor_reconciliation_task
        self._sensor_reconciliation_task = None
        if sensor_reconciliation_task is not None and (
            not sensor_reconciliation_task.done()
        ):
            sensor_reconciliation_task.cancel()
            try:
                await sensor_reconciliation_task
            except asyncio.CancelledError:
                pass
        insights_save_task = self._insights_save_task
        self._insights_save_task = None
        if insights_save_task is not None and (not insights_save_task.done()):
            try:
                await insights_save_task
            except asyncio.CancelledError:
                pass
        self._insights_save_dirty = True
        await self._async_save_insights_learning()
        await self._async_shutdown_coordinator()
        self.recommendation_engine.disconnect()
        self.provider.attach_runtime(None)
        self._status = "stopped"
        self._shutdown_finalized = True

    async def _async_shutdown_coordinator(self) -> None:
        coordinator = self.coordinator
        self.coordinator = None
        if coordinator is None:
            return
        try:
            await coordinator.async_shutdown()
        except Exception:
            _LOGGER.exception("EAI coordinator shutdown failed")
        return None if True else None

    def status_snapshot(self) -> dict[str, Any]:
        """Return a secret-free additive provider status payload."""
        coordinator = self.coordinator
        ai_predictor = getattr(coordinator, "ai_predictor", None)
        ai_state = getattr(getattr(ai_predictor, "state", None), "value", None)
        if not self._dependencies_ok:
            ai_mode = "unavailable"
        elif getattr(coordinator, "_ml_ready", False):
            ai_mode = "hybrid_ai"
        else:
            ai_mode = "rule_based_learning"
        return {
            "status": self._status,
            "coordinator_active": coordinator is not None,
            "database_active": bool(
                coordinator is not None
                and getattr(
                    getattr(coordinator, "data_manager", None), "_initialized", False
                )
            ),
            "weather_pipeline_active": bool(
                getattr(
                    getattr(coordinator, "weather_pipeline_manager", None),
                    "_running",
                    False,
                )
            ),
            "ai_mode": ai_mode,
            "ai_state": ai_state,
            "heat_pump_type": getattr(coordinator, "wp_type", None),
            "rated_cop": getattr(coordinator, "cop_rated", None),
            "failure_code": self._failure_code,
            "weather_intelligence": {
                "enabled": self._weather_engine is not None,
                "status": self.weather_intelligence_snapshot.get("status"),
                "source_status": self.weather_intelligence_snapshot.get(
                    "source_status"
                ),
            },
        }
