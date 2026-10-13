# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import (
    BACKFILL_DAYS,
    CONF_BASE_FEE_EUR_MONTH,
    CONF_BATTERY_POWER_SENSOR,
    CONF_COUNTRY,
    CONF_CSV_COMMUNITY,
    CONF_FEED_IN_TARIFF_CT,
    CONF_GRID_FEE,
    CONF_MAX_PRICE,
    CONF_FORCE_CHARGE_PRICE,
    CONF_THRESHOLD_MODE,
    CONF_BELOW_AVERAGE_PCT,
    CONF_CHEAPEST_HOURS,
    CONF_PROVIDER_MARKUP,
    CONF_TARIFF_MODE,
    CONF_TAXES_FEES,
    CONF_TIME_OF_USE,
    CONF_VAT_RATE,
    DB_PATH,
    DEFAULT_BASE_FEE_EUR_MONTH,
    DEFAULT_COUNTRY,
    DEFAULT_FEED_IN_TARIFF_CT,
    DEFAULT_GRID_FEE,
    DEFAULT_MAX_PRICE,
    DEFAULT_FORCE_CHARGE_PRICE,
    DEFAULT_THRESHOLD_MODE,
    DEFAULT_BELOW_AVERAGE_PCT,
    DEFAULT_CHEAPEST_HOURS,
    DEFAULT_PROVIDER_MARKUP,
    DEFAULT_TAXES_FEES,
    DOMAIN,
    PRICE_FETCH_INTERVAL,
    SIGNAL_PRICES_REVISED,
    TARIFF_MODE_CSV_COMMUNITY,
    TARIFF_MODE_DEMO,
    TARIFF_MODE_DYNAMIC,
    TARIFF_MODE_TIME_OF_USE,
    TARIFF_MODE_TIME_WINDOWS,
    THRESHOLD_MODES,
    UPDATE_INTERVAL,
    VAT_RATE_AT,
    VAT_RATE_DE,
)
from .core import ElectricityPriceService, BatteryTracker, PriceCalculator
from .core.thresholds import (
    DayThresholds,
    THRESHOLD_LIMITS,
    ThresholdConfig,
    evaluate_current,
    evaluate_day,
    merge_and_validate_thresholds,
    slot_is_cheap,
    threshold_options,
    thresholds_payload,
)
from .helpers import GPMLogger, async_setup_gpm_logging
from .license import (
    has_corrections,
    has_csv_import,
    has_tariff_models,
    is_demo_entitlements,
    mask_license_id,
    resolve_entitlements,
)
from .storage import (
    DataValidator,
    GPMDatabaseConnector,
    HistoryManager,
    PriceCache,
    PriceWriter,
    StatisticsStore,
)
from .storage.demo_persist import async_persist_demo_prices
from .tariffs.base import week_schedule_prices
from .tariffs.demo import DemoSource, demo_window
from .tariffs.factory import build_price_source

_LOGGER = logging.getLogger(__name__)


class GridPriceMonitorCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Solar Forecast GPM data updates @zara"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator @zara"""
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.entry = entry
        config = self._entry_config()
        self._country = config.get(CONF_COUNTRY, DEFAULT_COUNTRY)
        self._grid_fee = config.get(CONF_GRID_FEE, DEFAULT_GRID_FEE)
        self._taxes_fees = config.get(CONF_TAXES_FEES, DEFAULT_TAXES_FEES)
        self._provider_markup = config.get(
            CONF_PROVIDER_MARKUP, DEFAULT_PROVIDER_MARKUP
        )
        self._threshold_config = self._threshold_config_from_entry()
        self._max_price = self._threshold_config.max_price
        default_vat_rate = VAT_RATE_AT if self._country == "AT" else VAT_RATE_DE
        self._vat_rate = config.get(CONF_VAT_RATE, default_vat_rate)
        self._calculator = PriceCalculator(
            vat_rate=self._vat_rate,
            grid_fee=self._grid_fee,
            taxes_fees=self._taxes_fees,
            provider_markup=self._provider_markup,
        )
        self._price_service = ElectricityPriceService(self._country)
        self._last_price_fetch = None
        self._write_lock = asyncio.Lock()
        self._price_writer = None
        self._price_source = None
        self._source_fingerprint = None
        self._last_slots_fingerprint = None
        self._last_backfill_date = None
        self._price_revision = 0
        self._last_correction = None
        self._battery_power_sensor = config.get(CONF_BATTERY_POWER_SENSOR, "")
        self._battery_tracker = None
        self._db_connector = None
        self._data_validator = None
        self._price_cache = None
        self._history_manager = None
        self._statistics_store = None
        self._gpm_logger = None
        self._storage_initialized = False
        self._license_task = None
        self._license_source = None
        self._license_id = None
        self._license_expires_at = None
        self._entitlement_fingerprint = None
        self._refresh_entitlements()

    def _entry_config(self) -> dict[str, Any]:
        config = {**self.entry.data, **self.entry.options}
        config.setdefault(CONF_TARIFF_MODE, TARIFF_MODE_DYNAMIC)
        return config

    def _refresh_entitlements(self) -> None:
        license_state = resolve_entitlements(self.hass, self.entry)
        fingerprint = (
            license_state.status,
            license_state.entitlements,
            license_state.source,
        )
        previous_fingerprint = self._entitlement_fingerprint
        self._license_status = license_state.status
        self._entitlements = license_state.entitlements
        self._license_source = license_state.source
        self._license_id = license_state.license_id
        self._license_expires_at = license_state.expires_at
        self._is_demo = is_demo_entitlements(license_state.entitlements)
        if self._is_demo:
            self._tariff_mode = TARIFF_MODE_DEMO
        else:
            config = self._entry_config()
            self._tariff_mode = config.get(CONF_TARIFF_MODE, TARIFF_MODE_DYNAMIC)
        self._entitlement_fingerprint = fingerprint
        if previous_fingerprint is not None and previous_fingerprint != fingerprint:
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.entry.entry_id),
                name=f"{DOMAIN}_entitlement_reload_{self.entry.entry_id}",
            )
        return None if True else None

    @property
    def is_demo(self) -> bool:
        return self._is_demo

    @property
    def entitlements(self) -> frozenset[str]:
        return self._entitlements

    @property
    def tariff_mode(self) -> str:
        return self._tariff_mode

    def snapshot(self) -> dict[str, Any]:
        """Read-only provider snapshot. Never includes key material."""
        config = self._entry_config()
        data = self.data or {}
        license_expiry_text = None
        if self._license_expires_at is not None:
            license_expiry_text = self._license_expires_at.isoformat()
        country = config.get(CONF_COUNTRY, DEFAULT_COUNTRY)
        feed_in_tariff_ct = config.get(CONF_FEED_IN_TARIFF_CT)
        base_fee_eur_month = config.get(CONF_BASE_FEE_EUR_MONTH)
        if self._tariff_mode == TARIFF_MODE_DEMO:
            tariff_label = "Demo"
            has_spot_component = False
        elif self._price_source is not None:
            tariff_label = self._price_source.label
            has_spot_component = bool(self._price_source.has_spot_component)
        elif self._tariff_mode == TARIFF_MODE_DYNAMIC:
            tariff_label = f"Dynamisch (aWATTar {country})"
            has_spot_component = True
        else:
            tariff_label = str(self._tariff_mode)
            has_spot_component = False
        return {
            "provider_version": 1,
            "license_status": self._license_status,
            "license_source": self._license_source,
            "license_id_masked": mask_license_id(self._license_id),
            "license_expires_at": license_expiry_text,
            "entitlements": sorted(self._entitlements),
            "is_demo": self._is_demo,
            "tariff_mode": self._tariff_mode,
            "tariff_label": tariff_label,
            "country": country,
            "has_spot_component": has_spot_component,
            "price_revision": int(
                data.get("price_revision")
                if data.get("price_revision") is not None
                else self._price_revision
            ),
            "feed_in_tariff_ct": (
                None if feed_in_tariff_ct in (None, "") else float(feed_in_tariff_ct)
            ),
            "base_fee_eur_month": (
                None if base_fee_eur_month in (None, "") else float(base_fee_eur_month)
            ),
            "price_components": {
                "vat_rate": int(self._vat_rate),
                "grid_fee": float(self._grid_fee),
                "taxes_fees": float(self._taxes_fees),
                "provider_markup": float(self._provider_markup),
            },
            "tariff_schedule": self._tariff_schedule_snapshot(),
            "last_correction": self._last_correction,
            "thresholds": data.get("thresholds")
            or thresholds_payload(self._threshold_config),
            "effective_threshold_ct": data.get("effective_threshold_ct"),
            "capabilities": {
                "tariff_models": has_tariff_models(self._entitlements),
                "csv_import": has_csv_import(self._entitlements),
                "corrections": has_corrections(self._entitlements),
            },
            "last_update": data.get("last_update"),
        }

    def _tariff_schedule_snapshot(self) -> dict[str, Any] | None:
        if self._tariff_mode not in (TARIFF_MODE_TIME_OF_USE, TARIFF_MODE_TIME_WINDOWS):
            return
        price_source = self._price_source
        if price_source is None or not hasattr(price_source, "price_for"):
            return None
        local_now = datetime.now().astimezone().replace(tzinfo=None)
        week_start = (local_now - timedelta(days=local_now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        holiday_hint = None
        if self._tariff_mode == TARIFF_MODE_TIME_OF_USE:
            time_of_use_config = self._entry_config().get(CONF_TIME_OF_USE) or {}
            if time_of_use_config.get("holiday_low"):
                holiday_hint = "holiday_low"
        return {
            "days": week_schedule_prices(price_source, week_start),
            "holiday_hint": holiday_hint,
        }

    def schedule_license_monitor(self, expires_at: datetime | None) -> None:
        if expires_at is None:
            return
        self._license_task = self.hass.async_create_task(
            self._async_monitor_license(expires_at),
            name=f"{DOMAIN}_license_{self.entry.entry_id}",
        )

    async def _async_monitor_license(self, expires_at: datetime) -> None:
        from .license import resolve_entitlements as resolve_entitlements
        from .license.models import LicenseStatus as LicenseStatus

        retry_seconds = 60.0
        while True:
            utc_now = datetime.now(timezone.utc)
            expiry_utc = (
                expires_at.astimezone(timezone.utc)
                if expires_at.tzinfo
                else expires_at.replace(tzinfo=timezone.utc)
            )
            await asyncio.sleep(
                max(0.0, min(3600.0, (expiry_utc - utc_now).total_seconds()))
            )
            try:
                license_state = resolve_entitlements(self.hass, self.entry)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "GPM license revalidation failed; retrying in %.0f seconds",
                    retry_seconds,
                )
                await asyncio.sleep(retry_seconds)
                retry_seconds = min(retry_seconds * 2, 3600.0)
                continue
            retry_seconds = 60.0
            if license_state.status == LicenseStatus.VALID.value:
                if license_state.expires_at is None:
                    return
                expires_at = license_state.expires_at
                continue
            await self.hass.config_entries.async_reload(self.entry.entry_id)
            return

    def schedule_not_yet_valid_recheck(self) -> None:
        self._license_task = self.hass.async_create_task(
            self._async_recheck_not_yet_valid(),
            name=f"{DOMAIN}_license_recheck_{self.entry.entry_id}",
        )

    async def _async_recheck_not_yet_valid(self) -> None:
        from homeassistant.const import (
            EVENT_HOMEASSISTANT_STARTED as EVENT_HOMEASSISTANT_STARTED,
        )
        from .const import (
            LICENSE_RECHECK_INTERVAL_SECONDS as LICENSE_RECHECK_INTERVAL_SECONDS,
            LICENSE_RECHECK_WINDOW_SECONDS as LICENSE_RECHECK_WINDOW_SECONDS,
        )
        from .license import resolve_entitlements as resolve_entitlements
        from .license.models import LicenseStatus as LicenseStatus

        recheck_deadline = datetime.now(timezone.utc) + timedelta(
            seconds=LICENSE_RECHECK_WINDOW_SECONDS
        )

        async def _attempt() -> bool:
            license_state = resolve_entitlements(self.hass, self.entry)
            if (
                license_state.status == LicenseStatus.VALID.value
                and license_state.entitlements
            ):
                await self.hass.config_entries.async_reload(self.entry.entry_id)
                return True
            return False

        def _on_started(_event) -> None:
            self.hass.async_create_task(_attempt())

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)
        while datetime.now(timezone.utc) < recheck_deadline:
            await asyncio.sleep(LICENSE_RECHECK_INTERVAL_SECONDS)
            if await _attempt():
                return

    @property
    def vat_factor(self) -> float:
        """Get VAT factor (e.g. 1.19 for 19%) @zara"""
        return self._calculator.vat_factor

    @property
    def total_markup(self) -> float:
        """Calculate total markup (grid fee + taxes + provider) - all gross @zara"""
        return self._calculator.total_markup

    @property
    def max_price(self) -> float:
        """Get the configured max price threshold @zara"""
        return self._max_price

    @property
    def battery_tracker(self) -> BatteryTracker | None:
        """Get the battery tracker instance @zara"""
        return self._battery_tracker

    @property
    def has_battery_sensor(self) -> bool:
        """Check if battery power sensor is configured @zara"""
        return bool(self._battery_power_sensor)

    @property
    def gpm_logger(self) -> GPMLogger | None:
        """Get the GPM logger instance @zara"""
        return self._gpm_logger

    async def async_initialize_storage(self) -> None:
        """Initialize persistent storage components @zara"""
        if self._storage_initialized:
            return
        config_path = Path(self.hass.config.path())
        logging_directory = config_path / "grid_price_monitor"
        solar_db_path = self.hass.config.path("solar_forecast_ml/solar_forecast.db")
        self._db_connector = GPMDatabaseConnector(solar_db_path)
        await self._db_connector.connect()
        self._data_validator = DataValidator(
            logging_directory, self._db_connector, self.hass
        )
        await self._data_validator.async_validate_structure()
        self._price_cache = PriceCache(self._db_connector)
        self._history_manager = HistoryManager(self._db_connector)
        self._statistics_store = StatisticsStore(self._db_connector)
        self._gpm_logger = await async_setup_gpm_logging(
            self._data_validator.logs_path, self.hass
        )
        await self._price_cache.async_load()
        if self._is_demo:
            await self._price_cache.async_clear()
        else:
            cached_tariff_mode = None
            try:
                cache_metadata = await self._db_connector.fetchone(
                    "SELECT tariff_mode FROM GPM_price_cache_meta WHERE id = 1"
                )
                cached_tariff_mode = (
                    cache_metadata["tariff_mode"] if cache_metadata else None
                )
            except Exception:
                cached_tariff_mode = None
            if cached_tariff_mode == TARIFF_MODE_DEMO:
                await self._price_cache.async_clear()
            elif await self._price_cache.async_is_valid():
                cached_prices = await self._price_cache.async_get_prices()
                if cached_prices:
                    self._price_service.set_prices_from_cache(cached_prices)
                    _LOGGER.debug("Loaded %d prices from cache", len(cached_prices))
                    if self._gpm_logger:
                        self._gpm_logger.info(
                            "Loaded %d prices from cache", len(cached_prices)
                        )
        await self._history_manager.async_load()
        await self._statistics_store.async_load()
        self._storage_initialized = True
        self._price_writer = PriceWriter(
            self._db_connector,
            self._price_cache,
            lock=self._write_lock,
            dispatcher=lambda payload: async_dispatcher_send(
                self.hass, SIGNAL_PRICES_REVISED, payload
            ),
            signal=SIGNAL_PRICES_REVISED,
        )
        self._rebuild_price_source()
        await self._async_backfill_if_needed()
        _LOGGER.info("GPM storage initialized (database: %s)", DB_PATH)

    async def async_shutdown_storage(self) -> None:
        """Shutdown storage components @zara"""
        if self._license_task is not None:
            self._license_task.cancel()
            self._license_task = None
        if self._gpm_logger:
            self._gpm_logger.shutdown()
            self._gpm_logger = None
        if self._db_connector:
            await self._db_connector.close()
            self._db_connector = None
        self._storage_initialized = False

    async def async_setup_battery_tracker(self) -> None:
        """Initialize the battery tracker if configured @zara"""
        if self._is_demo:
            return
        if self._battery_power_sensor:
            self._battery_tracker = BatteryTracker(
                self.hass, self.entry.entry_id, db=self._db_connector
            )
            await self._battery_tracker.async_setup(self._battery_power_sensor)
            _LOGGER.debug(
                "Battery tracker initialized for sensor: %s", self._battery_power_sensor
            )
        return None if True else None

    async def async_shutdown_battery_tracker(self) -> None:
        """Shutdown the battery tracker @zara"""
        if self._battery_tracker:
            await self._battery_tracker.async_unload()
            self._battery_tracker = None
        return None if True else None

    def calculate_total_price(self, spot_price_net: float) -> float:
        "Calculate total gross price from net spot price @zara\n\nFormula: (Spot_net x VAT_factor) + Grid_fee + Taxes + Provider_markup\n\nArgs:\n    spot_price_net: Net spot price in ct/kWh (from aWATTar)\n\nReturns:\n    Total gross price in ct/kWh\n"
        return self._calculator.calculate_total_price(spot_price_net)

    def calculate_spot_from_total(self, total_price: float) -> float:
        "Reverse calculate: get net spot price from total gross price @zara\n\nUsed for calibration and threshold calculations.\n\nArgs:\n    total_price: Total gross price in ct/kWh\n\nReturns:\n    Net spot price in ct/kWh\n"
        return self._calculator.calculate_spot_from_total(total_price)

    def update_config(self) -> None:
        """Update configuration values from entry @zara"""
        self._refresh_entitlements()
        config = self._entry_config()
        self._country = config.get(CONF_COUNTRY, DEFAULT_COUNTRY)
        self._grid_fee = config.get(CONF_GRID_FEE, DEFAULT_GRID_FEE)
        self._taxes_fees = config.get(CONF_TAXES_FEES, DEFAULT_TAXES_FEES)
        self._provider_markup = config.get(
            CONF_PROVIDER_MARKUP, DEFAULT_PROVIDER_MARKUP
        )
        self._threshold_config = self._threshold_config_from_entry()
        self._max_price = self._threshold_config.max_price
        self._battery_power_sensor = config.get(CONF_BATTERY_POWER_SENSOR, "")
        default_vat_rate = VAT_RATE_AT if self._country == "AT" else VAT_RATE_DE
        self._vat_rate = config.get(CONF_VAT_RATE, default_vat_rate)
        self._calculator.update_config(
            vat_rate=self._vat_rate,
            grid_fee=self._grid_fee,
            taxes_fees=self._taxes_fees,
            provider_markup=self._provider_markup,
        )
        _LOGGER.debug(
            "Configuration updated: markup=%.2f, vat=%d%%, max_price=%.2f",
            self.total_markup,
            self._vat_rate,
            self._max_price,
        )
        if self._gpm_logger:
            self._gpm_logger.log_config_change(
                {
                    "vat_rate": self._vat_rate,
                    "grid_fee": self._grid_fee,
                    "taxes_fees": self._taxes_fees,
                    "provider_markup": self._provider_markup,
                    "max_price": self._max_price,
                }
            )
        self._rebuild_price_source()

    def _threshold_config_from_entry(self) -> ThresholdConfig:
        config = self._entry_config()
        threshold_mode = config.get(CONF_THRESHOLD_MODE, DEFAULT_THRESHOLD_MODE)
        if threshold_mode not in THRESHOLD_MODES:
            threshold_mode = DEFAULT_THRESHOLD_MODE
        return ThresholdConfig(
            mode=str(threshold_mode),
            max_price=float(config.get(CONF_MAX_PRICE, DEFAULT_MAX_PRICE)),
            force_charge_price=float(
                config.get(CONF_FORCE_CHARGE_PRICE, DEFAULT_FORCE_CHARGE_PRICE)
            ),
            below_average_pct=float(
                config.get(CONF_BELOW_AVERAGE_PCT, DEFAULT_BELOW_AVERAGE_PCT)
            ),
            cheapest_hours=int(config.get(CONF_CHEAPEST_HOURS, DEFAULT_CHEAPEST_HOURS)),
        )

    async def async_update_thresholds(self, **changes: Any) -> dict[str, Any]:
        """Validate and persist price thresholds, then refresh coordinator data."""
        if self._is_demo:
            raise ValueError("demo_mode")
        new_threshold_config, validation_errors = merge_and_validate_thresholds(
            self._threshold_config, changes
        )
        if validation_errors:
            return {"errors": validation_errors}
        assert new_threshold_config is not None
        async with self._write_lock:
            entry_options = {
                **self.entry.options,
                **threshold_options(new_threshold_config),
            }
            self.hass.config_entries.async_update_entry(
                self.entry, options=entry_options
            )
            self.update_config()
        await self.async_request_refresh()
        threshold_payload = (self.data or {}).get("thresholds")
        if not threshold_payload:
            threshold_payload = thresholds_payload(self._threshold_config)
        return dict(threshold_payload)

    def _rebuild_price_source(self) -> None:
        config = self._entry_config()
        price_source = build_price_source(
            config, self._entitlements, price_service=self._price_service
        )
        fingerprint = (
            price_source.mode,
            price_source.label,
            price_source.has_spot_component,
            config.get(CONF_VAT_RATE),
            config.get(CONF_GRID_FEE),
            config.get(CONF_TAXES_FEES),
            config.get(CONF_PROVIDER_MARKUP),
            config.get("fixed"),
            config.get("time_of_use"),
            config.get("time_windows"),
            config.get("csv_community"),
        )
        if (
            self._source_fingerprint is not None
            and fingerprint != self._source_fingerprint
        ):
            self._last_price_fetch = None
        self._source_fingerprint = fingerprint
        self._price_source = price_source
        if price_source.mode != TARIFF_MODE_DEMO:
            self._tariff_mode = price_source.mode
        return None if True else None

    def _data_source_label(self) -> str:
        price_source = self._price_source
        if price_source is None or price_source.mode == TARIFF_MODE_DYNAMIC:
            return "aWATTar"
        if price_source.mode == TARIFF_MODE_DEMO:
            return "demo"
        if price_source.mode == TARIFF_MODE_CSV_COMMUNITY:
            return f"csv+{price_source.label}"
        return f"tariff:{price_source.mode}"

    async def _async_backfill_if_needed(self) -> None:
        price_source = self._price_source
        price_writer = self._price_writer
        if (
            price_source is None
            or price_writer is None
            or self._is_demo
            or price_source.needs_network
            or (price_source.mode == TARIFF_MODE_DEMO)
        ):
            return None
        today = datetime.now().astimezone().date()
        if self._last_backfill_date == today:
            return
        local_now = datetime.now().astimezone().replace(tzinfo=None)
        window_start = local_now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=BACKFILL_DAYS)
        window_end = local_now.replace(minute=0, second=0, microsecond=0) + timedelta(
            hours=1
        )
        fetched_slots = await price_source.async_get_slots(window_start, window_end)
        if fetched_slots:
            write_result = await price_writer.backfill_missing(
                fetched_slots, now_local=local_now
            )
            self._price_revision = write_result.revision
        self._last_backfill_date = today

    async def async_import_csv_path(
        self,
        path: str,
        *,
        profile: str,
        price_unit: str,
        csv_timezone: str,
        column_map: dict[str, str] | None,
    ) -> dict[str, Any]:
        csv_bytes = await self.hass.async_add_executor_job(Path(path).read_bytes)
        return await self.async_import_csv_bytes(
            csv_bytes,
            profile=profile,
            price_unit=price_unit,
            csv_timezone=csv_timezone,
            column_map=column_map,
        )

    async def async_import_csv_bytes(
        self,
        data: bytes,
        *,
        profile: str,
        price_unit: str,
        csv_timezone: str,
        column_map: dict[str, str] | None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        from .tariffs.csv_import import (
            PROFILE_COMMUNITY_SHARE as PROFILE_COMMUNITY_SHARE,
            CsvImportError as CsvImportError,
            apply_csv_hours as apply_csv_hours,
            default_local_tz as default_local_tz,
            preview_against_history as preview_against_history,
        )

        if self._is_demo or not has_csv_import(self._entitlements):
            raise CsvImportError("csv_license_required")
        if self._db_connector is None:
            raise CsvImportError("csv_not_ready")
        local_tz = default_local_tz(getattr(self.hass.config, "time_zone", None))
        clock_now = now or datetime.now(tz=local_tz)
        config = self._entry_config()
        community_config = config.get(CONF_CSV_COMMUNITY) or {}
        eeg_price = community_config.get("eeg_price")
        parsed_csv = await self._parse_csv_bytes(
            data,
            profile=profile,
            price_unit=price_unit,
            csv_timezone=csv_timezone,
            column_map=column_map,
            local_tz=local_tz,
            now=clock_now,
            eeg_price=eeg_price,
            base_prices=None,
        )
        if parsed_csv.profile == PROFILE_COMMUNITY_SHARE:
            group_timestamps = list(parsed_csv.groups)
            base_prices = await self._csv_base_prices(group_timestamps)
            parsed_csv = await self._parse_csv_bytes(
                data,
                profile=parsed_csv.profile,
                price_unit=parsed_csv.unit,
                csv_timezone=csv_timezone,
                column_map=parsed_csv.column_map,
                local_tz=local_tz,
                now=clock_now,
                eeg_price=eeg_price,
                base_prices=base_prices,
            )
        if not parsed_csv.hours:
            raise CsvImportError("csv_no_hours")
        preview_comparison = await preview_against_history(
            self._db_connector, parsed_csv.hours
        )
        async with self._write_lock:
            write_result = await apply_csv_hours(
                self._db_connector,
                parsed_csv.hours,
                stats_store=self._statistics_store,
                params={
                    "profile": parsed_csv.profile,
                    "price_unit": parsed_csv.unit,
                    "timezone": csv_timezone,
                    "column_map": parsed_csv.column_map,
                },
                source_hash=parsed_csv.source_hash,
                now=clock_now,
            )
        self._price_revision = write_result["revision"]
        self._last_correction = {
            "id": write_result["correction_id"],
            "kind": "csv",
            "month": None,
            "applied_at": write_result["applied_at"],
        }
        async_dispatcher_send(
            self.hass,
            SIGNAL_PRICES_REVISED,
            {
                "revision": write_result["revision"],
                "from": write_result["from"],
                "to": write_result["to"],
                "reason": "csv",
            },
        )
        write_result.update(
            {
                "detected_unit": parsed_csv.detected_unit,
                "profile": parsed_csv.profile,
                "row_count": parsed_csv.row_count,
                "skipped_rows": parsed_csv.skipped_rows,
                "ignored_old": parsed_csv.ignored_old,
                "hours_matched": preview_comparison["hours_matched"],
                "hours_new": preview_comparison["hours_new"],
            }
        )
        return write_result

    async def async_preview_csv_bytes(
        self,
        data: bytes,
        *,
        profile: str,
        price_unit: str,
        csv_timezone: str,
        column_map: dict[str, str] | None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        from .tariffs.csv_import import (
            PROFILE_COMMUNITY_SHARE as PROFILE_COMMUNITY_SHARE,
            CsvImportError as CsvImportError,
            default_local_tz as default_local_tz,
            preview_against_history as preview_against_history,
        )

        if self._is_demo or not has_csv_import(self._entitlements):
            raise CsvImportError("csv_license_required")
        if self._db_connector is None:
            raise CsvImportError("csv_not_ready")
        local_tz = default_local_tz(getattr(self.hass.config, "time_zone", None))
        clock_now = now or datetime.now(tz=local_tz)
        config = self._entry_config()
        community_config = config.get(CONF_CSV_COMMUNITY) or {}
        parsed_csv = await self._parse_csv_bytes(
            data,
            profile=profile,
            price_unit=price_unit,
            csv_timezone=csv_timezone,
            column_map=column_map,
            local_tz=local_tz,
            now=clock_now,
            eeg_price=community_config.get("eeg_price"),
            base_prices=None,
        )
        if parsed_csv.profile == PROFILE_COMMUNITY_SHARE:
            base_prices = await self._csv_base_prices(list(parsed_csv.groups))
            parsed_csv = await self._parse_csv_bytes(
                data,
                profile=parsed_csv.profile,
                price_unit=parsed_csv.unit,
                csv_timezone=csv_timezone,
                column_map=parsed_csv.column_map,
                local_tz=local_tz,
                now=clock_now,
                eeg_price=community_config.get("eeg_price"),
                base_prices=base_prices,
            )
        preview_comparison = await preview_against_history(
            self._db_connector, parsed_csv.hours
        )
        return {
            "parsed": parsed_csv,
            **preview_comparison,
            "range_from": parsed_csv.range_from,
            "range_to": parsed_csv.range_to,
            "row_count": parsed_csv.row_count,
            "detected_unit": parsed_csv.detected_unit,
            "profile": parsed_csv.profile,
        }

    def _correction_clock(self, now: datetime | None) -> tuple[Any, datetime]:
        from .tariffs.corrections import default_local_tz as default_local_tz

        local_tz = default_local_tz(getattr(self.hass.config, "time_zone", None))
        clock_now = now or datetime.now(tz=local_tz)
        if clock_now.tzinfo is None:
            clock_now = clock_now.replace(tzinfo=local_tz)
        return (local_tz, clock_now)

    async def async_preview_month_correction(
        self,
        *,
        year: int,
        month: int,
        billed_kwh: float,
        billed_eur: float,
        base_fee_eur: float = 0.0,
        method: str = "additive",
        weighting: str = "consumption",
        force: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        from .tariffs.corrections import (
            CorrectionError as CorrectionError,
            plan_month_correction as plan_month_correction,
        )

        if self._is_demo or not has_corrections(self._entitlements):
            raise CorrectionError("correction_license_required")
        if self._db_connector is None:
            raise CorrectionError("correction_not_ready")
        local_tz, clock_now = self._correction_clock(now)
        correction_plan = await plan_month_correction(
            self._db_connector,
            year=year,
            month=month,
            billed_kwh=billed_kwh,
            billed_eur=billed_eur,
            base_fee_eur=base_fee_eur,
            method=method,
            weighting=weighting,
            force=force,
            now=clock_now,
            local_tz=local_tz,
        )
        return correction_plan.as_preview()

    async def async_apply_month_correction(
        self,
        *,
        year: int,
        month: int,
        billed_kwh: float,
        billed_eur: float,
        base_fee_eur: float = 0.0,
        method: str = "additive",
        weighting: str = "consumption",
        force: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        from .tariffs.corrections import (
            CorrectionError as CorrectionError,
            apply_month_correction as apply_month_correction,
            plan_month_correction as plan_month_correction,
        )

        if self._is_demo or not has_corrections(self._entitlements):
            raise CorrectionError("correction_license_required")
        if self._db_connector is None:
            raise CorrectionError("correction_not_ready")
        local_tz, clock_now = self._correction_clock(now)
        correction_plan = await plan_month_correction(
            self._db_connector,
            year=year,
            month=month,
            billed_kwh=billed_kwh,
            billed_eur=billed_eur,
            base_fee_eur=base_fee_eur,
            method=method,
            weighting=weighting,
            force=force,
            now=clock_now,
            local_tz=local_tz,
        )
        async with self._write_lock:
            write_result = await apply_month_correction(
                self._db_connector,
                correction_plan,
                stats_store=self._statistics_store,
                now=clock_now,
            )
        self._price_revision = write_result["revision"]
        self._last_correction = {
            "id": write_result["correction_id"],
            "kind": "monthly",
            "month": write_result["month"],
            "applied_at": write_result["applied_at"],
        }
        async_dispatcher_send(
            self.hass,
            SIGNAL_PRICES_REVISED,
            {
                "revision": write_result["revision"],
                "from": write_result["from"],
                "to": write_result["to"],
                "reason": "monthly",
            },
        )
        return write_result

    async def async_revert_correction(
        self, correction_id: int, *, now: datetime | None = None
    ) -> dict[str, Any]:
        from .tariffs.corrections import (
            CorrectionError as CorrectionError,
            revert_correction as revert_correction,
        )

        if self._is_demo or not has_corrections(self._entitlements):
            raise CorrectionError("correction_license_required")
        if self._db_connector is None:
            raise CorrectionError("correction_not_ready")
        local_tz, clock_now = self._correction_clock(now)
        async with self._write_lock:
            write_result = await revert_correction(
                self._db_connector,
                int(correction_id),
                stats_store=self._statistics_store,
                now=clock_now,
            )
        self._price_revision = write_result["revision"]
        self._last_correction = {
            "id": write_result["correction_id"],
            "kind": write_result.get("kind"),
            "month": write_result.get("month"),
            "applied_at": None,
        }
        async_dispatcher_send(
            self.hass,
            SIGNAL_PRICES_REVISED,
            {
                "revision": write_result["revision"],
                "from": write_result["from"],
                "to": write_result["to"],
                "reason": "revert",
            },
        )
        return write_result

    async def async_list_corrections(self) -> list[dict[str, Any]]:
        from .tariffs.corrections import (
            CorrectionError as CorrectionError,
            list_corrections as list_corrections,
        )

        if self._is_demo or not has_corrections(self._entitlements):
            raise CorrectionError("correction_license_required")
        if self._db_connector is None:
            raise CorrectionError("correction_not_ready")
        return await list_corrections(self._db_connector)

    async def _parse_csv_bytes(self, data: bytes, **kwargs: Any) -> Any:
        from .tariffs.csv_import import parse_csv_bytes as parse_csv_bytes

        return await self.hass.async_add_executor_job(
            partial(parse_csv_bytes, data, **kwargs)
        )

    async def _csv_base_prices(self, stamps: list[str]) -> dict[str, float]:
        if not stamps or self._db_connector is None:
            return {}
        query_placeholders = ",".join(("?" for timestamp in stamps))
        rows = await self._db_connector.fetchall(
            f"SELECT timestamp, COALESCE(total_price_raw, total_price) AS base\n                FROM GPM_price_history WHERE timestamp IN ({query_placeholders})",
            tuple(stamps),
        )
        return {
            row["timestamp"]: float(row["base"])
            for row in rows or []
            if row["base"] is not None
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch and process price data @zara"""
        self._refresh_entitlements()
        await self._async_backfill_if_needed()
        utc_now = datetime.now(timezone.utc)
        self.update_config()
        if self._is_demo:
            return await self._async_update_demo()
        price_source = self._price_source or build_price_source(
            self._entry_config(), self._entitlements, price_service=self._price_service
        )
        self._price_source = price_source
        local_now = datetime.now().astimezone().replace(tzinfo=None)
        fetch_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        fetch_end = fetch_start + timedelta(days=2)
        fetch_needed = (
            not price_source.needs_network
            or self._last_price_fetch is None
            or utc_now - self._last_price_fetch >= PRICE_FETCH_INTERVAL
            or (not self._price_service.has_data)
        )
        fetched_slots = None
        if fetch_needed:
            _LOGGER.debug("Fetching new price data from aWATTar API")
            fetched_slots = await price_source.async_get_slots(fetch_start, fetch_end)
            if fetched_slots is None:
                if not self._price_service.has_data:
                    if self._gpm_logger:
                        self._gpm_logger.log_api_fetch(
                            success=False,
                            country=self._country,
                            error="No cached data available",
                        )
                    raise UpdateFailed(
                        "Failed to fetch price data and no cached data available"
                    )
                _LOGGER.warning("Failed to fetch new prices, using cached data")
                if self._gpm_logger:
                    self._gpm_logger.log_api_fetch(
                        success=False, country=self._country, error="Using cached data"
                    )
            else:
                self._last_price_fetch = utc_now
                if self._gpm_logger:
                    self._gpm_logger.log_api_fetch(
                        success=True, country=self._country, entries=len(fetched_slots)
                    )
                if fetched_slots:
                    self._price_service.set_prices_from_cache(fetched_slots)
        price_source = self._price_source or price_source
        has_spot_component = bool(getattr(price_source, "has_spot_component", True))
        current_spot_net = (
            self._price_service.get_current_price() if has_spot_component else None
        )
        next_spot_net = (
            self._price_service.get_next_hour_price() if has_spot_component else None
        )
        current_total_price = None
        next_total_price = None
        current_spot_gross = None
        next_spot_gross = None
        price_slots = (
            fetched_slots
            if fetched_slots is not None
            else self._price_service.get_all_prices()
        )
        if has_spot_component and current_spot_net is not None:
            current_spot_gross = round(current_spot_net * self.vat_factor, 2)
            current_total_price = self.calculate_total_price(current_spot_net)
        elif not has_spot_component:
            current_slot = self._slot_for_hour(price_slots, local_now)
            next_slot = self._slot_for_hour(price_slots, local_now + timedelta(hours=1))
            current_total_price = (
                None if current_slot is None else float(current_slot.get("total_price"))
            )
            next_total_price = (
                None if next_slot is None else float(next_slot.get("total_price"))
            )
        if has_spot_component and next_spot_net is not None:
            next_spot_gross = round(next_spot_net * self.vat_factor, 2)
            next_total_price = self.calculate_total_price(next_spot_net)
        today_prices = self._price_service.get_today_prices()
        tomorrow_prices = self._price_service.get_tomorrow_prices()
        today_engine_slots = self._engine_slots(today_prices, has_spot_component)
        tomorrow_engine_slots = self._engine_slots(tomorrow_prices, has_spot_component)
        today_thresholds = evaluate_day(today_engine_slots, self._threshold_config)
        tomorrow_thresholds = evaluate_day(
            tomorrow_engine_slots, self._threshold_config
        )
        is_cheap, is_force_price = evaluate_current(
            current_total_price,
            local_now.hour,
            today_thresholds,
            self._threshold_config,
        )
        cheap_hours_today = list(today_thresholds.cheap_hours)
        cheap_hours_tomorrow = list(tomorrow_thresholds.cheap_hours)
        effective_threshold_ct = today_thresholds.effective_threshold_ct
        if self._gpm_logger and current_total_price is not None:
            self._gpm_logger.log_price_update(
                spot_price=current_spot_gross,
                total_price=current_total_price,
                is_cheap=is_cheap,
            )
        cheapest_entry = (
            self._price_service.get_cheapest_hour_today()
            if has_spot_component
            else None
        )
        most_expensive_entry = (
            self._price_service.get_most_expensive_hour_today()
            if has_spot_component
            else None
        )
        average_spot_net = (
            self._price_service.get_average_price_today()
            if has_spot_component
            else None
        )
        cheapest_total = None
        most_expensive_total = None
        average_total_price = None
        if today_prices:
            if has_spot_component:
                total_prices = [
                    self.calculate_total_price(price_entry["price"])
                    for price_entry in today_prices
                ]
            else:
                total_prices = [
                    float(price_entry.get("total_price", price_entry.get("price") or 0))
                    for price_entry in today_prices
                ]
            average_total_price = round(sum(total_prices) / len(total_prices), 2)
            if not has_spot_component or average_spot_net is None:
                average_spot_net = average_total_price
            cheapest_hour, most_expensive_hour, cheapest_total, most_expensive_total = (
                self._extrema_hours(today_prices, has_spot_component)
            )
            cheapest_entry = (
                {
                    "hour": cheapest_hour,
                    "price": (
                        self._spot_net_for_hour(today_prices, cheapest_hour)
                        if has_spot_component
                        else None
                    ),
                }
                if cheapest_hour is not None
                else None
            )
            most_expensive_entry = (
                {
                    "hour": most_expensive_hour,
                    "price": (
                        self._spot_net_for_hour(today_prices, most_expensive_hour)
                        if has_spot_component
                        else None
                    ),
                }
                if most_expensive_hour is not None
                else None
            )
            if self._statistics_store:
                if not self._is_demo:
                    await self._statistics_store.async_update_daily_average(
                        date=utc_now.date().isoformat(),
                        average_net=average_spot_net,
                        average_total=average_total_price,
                        min_price=cheapest_total,
                        max_price=most_expensive_total,
                    )
                    cheap_hour_count = len(today_thresholds.cheap_hours)
                    await self._statistics_store.async_update_monthly_summary(
                        year=utc_now.year,
                        month=utc_now.month,
                        average_price=average_total_price,
                        total_cheap_hours=cheap_hour_count,
                        country=self._country,
                    )
        else:
            cheapest_entry = None
            most_expensive_entry = None
        forecast_prices = today_prices
        forecast_today = self._build_forecast_with_total(
            forecast_prices, cheap_hours_today
        )
        forecast_tomorrow = self._build_forecast_with_total(
            tomorrow_prices, cheap_hours_tomorrow
        )
        next_cheap_hour = self._find_next_cheap_hour(
            today_thresholds, tomorrow_thresholds
        )
        price_trend = self._calculate_trend(current_total_price, next_total_price)
        data = {
            "spot_price_net": current_spot_net,
            "spot_price_next_hour_net": next_spot_net,
            "spot_price": current_spot_gross,
            "spot_price_next_hour": next_spot_gross,
            "total_price": current_total_price,
            "total_price_next_hour": next_total_price,
            "is_cheap": is_cheap,
            "is_force_price": is_force_price,
            "threshold_mode": self._threshold_config.mode,
            "effective_threshold_ct": effective_threshold_ct,
            "thresholds": thresholds_payload(self._threshold_config),
            "threshold_limits": THRESHOLD_LIMITS,
            "threshold_modes": list(THRESHOLD_MODES),
            "cheapest_hour_today": cheapest_entry["hour"] if cheapest_entry else None,
            "cheapest_price_today": cheapest_total,
            "cheapest_spot_net": (
                cheapest_entry["price"]
                if cheapest_entry and has_spot_component
                else None
            ),
            "most_expensive_hour_today": (
                most_expensive_entry["hour"] if most_expensive_entry else None
            ),
            "most_expensive_price_today": most_expensive_total,
            "most_expensive_spot_net": (
                most_expensive_entry["price"]
                if most_expensive_entry and has_spot_component
                else None
            ),
            "average_price_today": average_total_price,
            "average_spot_net": average_spot_net,
            "forecast_today": forecast_today,
            "forecast_tomorrow": forecast_tomorrow,
            "cheap_hours_today": cheap_hours_today,
            "cheap_hours_tomorrow": cheap_hours_tomorrow,
            "next_cheap_hour": next_cheap_hour["hour"] if next_cheap_hour else None,
            "next_cheap_timestamp": (
                next_cheap_hour["timestamp"].isoformat() if next_cheap_hour else None
            ),
            "price_trend": price_trend,
            "last_update": utc_now.isoformat(),
            "data_source": "aWATTar",
            "country": self._country,
            "markup_total": self.total_markup,
            "vat_rate": self._vat_rate,
            "vat_factor": self.vat_factor,
            "max_price_threshold": self._max_price,
            "tariff_mode": (
                price_source.mode if price_source is not None else self._tariff_mode
            ),
            "tariff_label": (
                price_source.label
                if price_source is not None
                else f"Dynamisch (aWATTar {self._country})"
            ),
            "has_spot_component": has_spot_component,
            "price_revision": self._price_revision,
            "is_demo": False,
            "feed_in_tariff_ct": self._entry_config().get(CONF_FEED_IN_TARIFF_CT),
            "base_fee_eur_month": self._entry_config().get(CONF_BASE_FEE_EUR_MONTH),
        }
        if price_source is not None and price_source.mode != TARIFF_MODE_DYNAMIC:
            data["data_source"] = self._data_source_label()
        config = self._entry_config()
        feed_in_tariff_ct = config.get(CONF_FEED_IN_TARIFF_CT)
        base_fee_eur_month = config.get(CONF_BASE_FEE_EUR_MONTH)
        if self._price_writer:
            fingerprint = self._slots_fingerprint(price_slots)
            slots_changed = (
                fetched_slots is not None
                and fingerprint != self._last_slots_fingerprint
            )
            write_result = await self._price_writer.write_slots(
                price_slots or [],
                mode=(
                    price_source.mode
                    if price_source is not None
                    else TARIFF_MODE_DYNAMIC
                ),
                country=self._country,
                is_demo=False,
                now_local=local_now,
                feed_in_tariff_ct=(
                    None
                    if feed_in_tariff_ct in (None, "")
                    else float(feed_in_tariff_ct)
                ),
                base_fee_eur_month=(
                    None
                    if base_fee_eur_month in (None, "")
                    else float(base_fee_eur_month)
                ),
                reason="config",
                persist_history=slots_changed,
                current_price={
                    "spot_price_net": current_spot_net,
                    "spot_price": current_spot_gross,
                    "total_price": current_total_price,
                    "total_price_next_hour": next_total_price,
                    "is_cheap": is_cheap,
                    "average_today": average_total_price,
                    "cheapest_today": cheapest_total,
                    "most_expensive_today": most_expensive_total,
                },
            )
            if slots_changed:
                self._last_slots_fingerprint = fingerprint
            self._price_revision = write_result.revision
            data["price_revision"] = write_result.revision
        if self._battery_power_sensor:
            battery_power = self._get_battery_power()
            data["battery_power"] = battery_power
            if self._battery_tracker:
                battery_statistics = self._battery_tracker.get_statistics()
                battery_statistics["battery_power"] = battery_power
                data.update(battery_statistics)
                if self._gpm_logger:
                    self._gpm_logger.log_battery_charge(
                        power_w=battery_power,
                        energy_today_kwh=battery_statistics.get(
                            "battery_charged_today", 0
                        ),
                        energy_month_kwh=battery_statistics.get(
                            "battery_charged_month", 0
                        ),
                    )
                if self._statistics_store:
                    await self._statistics_store.async_update_battery_totals(
                        today_kwh=battery_statistics.get("battery_charged_today", 0),
                        week_kwh=battery_statistics.get("battery_charged_week", 0),
                        month_kwh=battery_statistics.get("battery_charged_month", 0),
                    )
        return data

    async def _async_update_demo(self) -> dict[str, Any]:
        """Build demo coordinator data and persist cache/current_price only."""
        local_now = datetime.now().astimezone().replace(tzinfo=None)
        window_start, window_end = demo_window(local_now)
        fetched_slots = await DemoSource().async_get_slots(window_start, window_end)
        self._price_service.set_prices_from_cache(fetched_slots)
        config = self._entry_config()
        feed_in_tariff_ct = config.get(
            CONF_FEED_IN_TARIFF_CT, DEFAULT_FEED_IN_TARIFF_CT
        )
        base_fee_eur_month = config.get(
            CONF_BASE_FEE_EUR_MONTH, DEFAULT_BASE_FEE_EUR_MONTH
        )
        if self._price_cache and self._db_connector:
            await async_persist_demo_prices(
                self._db_connector,
                self._price_cache,
                fetched_slots,
                country=self._country,
                now_local=local_now,
                feed_in_tariff_ct=(
                    None
                    if feed_in_tariff_ct in (None, "")
                    else float(feed_in_tariff_ct)
                ),
                base_fee_eur_month=(
                    None
                    if base_fee_eur_month in (None, "")
                    else float(base_fee_eur_month)
                ),
            )
        today_slots = [
            slot
            for slot in fetched_slots
            if slot.get("date") == local_now.date().isoformat()
        ]
        tomorrow_slots = [
            slot
            for slot in fetched_slots
            if slot.get("date") == (local_now.date() + timedelta(days=1)).isoformat()
        ]
        current_slot = next(
            (slot for slot in today_slots if slot.get("hour") == local_now.hour),
            today_slots[0] if today_slots else None,
        )
        next_slot = None
        if current_slot is not None and current_slot in fetched_slots:
            current_slot_index = fetched_slots.index(current_slot)
            if current_slot_index + 1 < len(fetched_slots):
                next_slot = fetched_slots[current_slot_index + 1]
        total_price = None if current_slot is None else current_slot.get("total_price")
        next_total_price = None if next_slot is None else next_slot.get("total_price")
        today_totals = [float(slot["total_price"]) for slot in today_slots]
        average_today = (
            round(sum(today_totals) / len(today_totals), 2) if today_totals else None
        )
        forecast_today = [
            {
                "hour": slot["hour"],
                "spot_price_net": slot["price"],
                "spot_price": slot["price"],
                "total_price": slot["total_price"],
                "is_cheap": False,
            }
            for slot in today_slots
        ]
        forecast_tomorrow = [
            {
                "hour": slot["hour"],
                "spot_price_net": slot["price"],
                "spot_price": slot["price"],
                "total_price": slot["total_price"],
                "is_cheap": False,
            }
            for slot in tomorrow_slots
        ]
        return {
            "spot_price_net": None,
            "spot_price_next_hour_net": None,
            "spot_price": None,
            "spot_price_next_hour": None,
            "total_price": total_price,
            "total_price_next_hour": next_total_price,
            "is_cheap": False,
            "is_force_price": False,
            "threshold_mode": DEFAULT_THRESHOLD_MODE,
            "effective_threshold_ct": None,
            "thresholds": thresholds_payload(
                ThresholdConfig(
                    mode=DEFAULT_THRESHOLD_MODE,
                    max_price=DEFAULT_MAX_PRICE,
                    force_charge_price=DEFAULT_FORCE_CHARGE_PRICE,
                    below_average_pct=float(DEFAULT_BELOW_AVERAGE_PCT),
                    cheapest_hours=DEFAULT_CHEAPEST_HOURS,
                )
            ),
            "threshold_limits": THRESHOLD_LIMITS,
            "threshold_modes": list(THRESHOLD_MODES),
            "cheapest_hour_today": None,
            "cheapest_price_today": None,
            "cheapest_spot_net": None,
            "most_expensive_hour_today": None,
            "most_expensive_price_today": None,
            "most_expensive_spot_net": None,
            "average_price_today": average_today,
            "average_spot_net": None,
            "forecast_today": forecast_today,
            "forecast_tomorrow": forecast_tomorrow,
            "cheap_hours_today": [],
            "cheap_hours_tomorrow": [],
            "next_cheap_hour": None,
            "next_cheap_timestamp": None,
            "price_trend": "unknown",
            "last_update": datetime.now().isoformat(),
            "data_source": "demo",
            "country": self._country,
            "markup_total": self.total_markup,
            "vat_rate": self._vat_rate,
            "vat_factor": self.vat_factor,
            "max_price_threshold": self._max_price,
            "tariff_mode": TARIFF_MODE_DEMO,
            "tariff_label": "Demo",
            "has_spot_component": False,
            "price_revision": 0,
            "is_demo": True,
            "feed_in_tariff_ct": feed_in_tariff_ct,
            "base_fee_eur_month": base_fee_eur_month,
        }

    def _get_battery_power(self) -> float:
        """Get current battery power directly from sensor @zara"""
        if not self._battery_power_sensor:
            return 0.0
        battery_state = self.hass.states.get(self._battery_power_sensor)
        if battery_state is None or battery_state.state in ("unknown", "unavailable"):
            return 0.0
        try:
            return float(battery_state.state)
        except (ValueError, TypeError):
            _LOGGER.warning("Invalid battery power value: %s", battery_state.state)
            return 0.0

    def _slot_for_hour(
        self, slots: list[dict[str, Any]], hour_local: datetime
    ) -> dict[str, Any] | None:
        target_hour = hour_local.replace(minute=0, second=0, microsecond=0)
        for slot in slots:
            slot_timestamp = slot.get("timestamp")
            if isinstance(slot_timestamp, str):
                slot_timestamp = datetime.fromisoformat(slot_timestamp)
            if not isinstance(slot_timestamp, datetime):
                continue
            slot_timestamp = slot_timestamp.replace(
                tzinfo=None, minute=0, second=0, microsecond=0
            )
            if not slot_timestamp == target_hour:
                continue
            return slot

    @staticmethod
    def _slots_fingerprint(slots: list[dict[str, Any]] | None) -> tuple[str, ...]:
        fingerprint_parts = []
        for slot in slots or []:
            slot_timestamp = slot.get("timestamp")
            if isinstance(slot_timestamp, datetime):
                timestamp_text = slot_timestamp.replace(
                    tzinfo=None, minute=0, second=0, microsecond=0
                ).isoformat()
            else:
                timestamp_text = str(slot_timestamp)[:19]
            try:
                price_text = f"{round(float(slot.get('total_price')), 4):.4f}"
            except (TypeError, ValueError):
                price_text = "none"
            fingerprint_parts.append(f"{timestamp_text}:{price_text}")
        return tuple(fingerprint_parts)

    @staticmethod
    def _spot_net_for_hour(prices: list[dict[str, Any]], hour: int) -> float | None:
        for price_entry in prices:
            try:
                if int(price_entry.get("hour")) != hour:
                    continue
            except (TypeError, ValueError):
                continue
            spot_net = price_entry.get("price")
            if spot_net in (None, ""):
                return
            try:
                pass
                return float(spot_net)
            except (TypeError, ValueError):
                return

    def _slot_total(self, entry: dict[str, Any], has_spot: bool) -> float:
        if "total_price" in entry and entry["total_price"] is not None:
            return float(entry["total_price"])
        net_price = float(entry.get("price") or 0)
        if has_spot:
            return self.calculate_total_price(net_price)
        return net_price

    def _extrema_hours(
        self, prices: list[dict[str, Any]], has_spot: bool
    ) -> tuple[int | None, int | None, float | None, float | None]:
        if not prices:
            return (None, None, None, None)
        price_hours = [
            (self._slot_total(price_entry, has_spot), int(price_entry["hour"]))
            for price_entry in prices
        ]
        rounded_prices = [round(total_price, 4) for total_price, hour in price_hours]
        if max(rounded_prices) == min(rounded_prices):
            return (None, None, None, None)
        cheapest_total, cheapest_hour = min(price_hours, key=lambda item: item[0])
        most_expensive_price, most_expensive_hour = max(
            price_hours, key=lambda item: item[0]
        )
        return (
            cheapest_hour,
            most_expensive_hour,
            cheapest_total,
            most_expensive_price,
        )

    def _engine_slots(
        self, prices: list[dict[str, Any]], has_spot: bool
    ) -> list[tuple[int, float]]:
        engine_slots = []
        for price_entry in prices:
            try:
                hour = int(price_entry["hour"])
            except (KeyError, TypeError, ValueError):
                continue
            engine_slots.append((hour, self._slot_total(price_entry, has_spot)))
        return engine_slots

    def _build_forecast_with_total(
        self, prices: list[dict[str, Any]], cheap_hours: list[int]
    ) -> list[dict[str, Any]]:
        """Build forecast list with total prices @zara"""
        forecast = []
        cheap_hour_set = set(cheap_hours)
        has_spot_component = (
            True
            if self._price_source is None
            else bool(self._price_source.has_spot_component)
        )
        for price_entry in prices:
            total_price = self._slot_total(price_entry, has_spot_component)
            if has_spot_component:
                net_price = price_entry["price"]
                gross_price = round(net_price * self.vat_factor, 2)
            else:
                net_price = total_price
                gross_price = total_price
            forecast.append(
                {
                    "hour": price_entry["hour"],
                    "spot_price_net": net_price,
                    "spot_price": gross_price,
                    "total_price": total_price,
                    "is_cheap": price_entry["hour"] in cheap_hour_set,
                }
            )
        return forecast

    def _get_cheap_hours_for_date(self, prices: list[dict[str, Any]]) -> list[int]:
        """Get list of cheap hours for a date from the threshold engine."""
        has_spot_component = (
            True
            if self._price_source is None
            else bool(self._price_source.has_spot_component)
        )
        day_thresholds = evaluate_day(
            self._engine_slots(prices, has_spot_component), self._threshold_config
        )
        return list(day_thresholds.cheap_hours)

    def _find_next_cheap_hour(
        self, today_thresholds: DayThresholds, tomorrow_thresholds: DayThresholds
    ) -> dict[str, Any] | None:
        """Find the next hour marked cheap by the threshold engine."""
        local_now = datetime.now()
        today = local_now.date()
        tomorrow = today + timedelta(days=1)
        has_spot_component = (
            True
            if self._price_source is None
            else bool(self._price_source.has_spot_component)
        )
        for price_entry in self._price_service.get_all_prices():
            timestamp = price_entry["timestamp"]
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)
            if timestamp.tzinfo is not None:
                local_timestamp = timestamp.astimezone().replace(tzinfo=None)
            else:
                local_timestamp = timestamp
            if local_timestamp < local_now:
                continue
            slot_day = local_timestamp.date()
            if slot_day == today:
                applicable_thresholds = today_thresholds
            elif slot_day == tomorrow:
                applicable_thresholds = tomorrow_thresholds
            else:
                continue
            total_price = self._slot_total(price_entry, has_spot_component)
            if not slot_is_cheap(
                price_entry["hour"],
                total_price,
                applicable_thresholds,
                self._threshold_config,
            ):
                continue
            return {
                "hour": price_entry["hour"],
                "timestamp": local_timestamp,
                "total_price": total_price,
            }

    def _calculate_trend(self, current: float | None, next_hour: float | None) -> str:
        """Calculate price trend based on total prices @zara"""
        if current is None or next_hour is None:
            return "unknown"
        price_delta = next_hour - current
        if price_delta > 1:
            return "rising"
        if price_delta < -1:
            return "falling"
        return "stable"
