# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""REST API views for SFML Stats dashboard. @zara"""

from __future__ import annotations
import logging
from pathlib import Path
from homeassistant.core import HomeAssistant
from . import context
from .common import (
    APIContext,
    FRONTEND_IMMUTABLE_HEADERS,
    FRONTEND_NO_STORE_HEADERS,
    FRONTEND_RELEASE_ID_PATTERN,
    FRONTEND_RELEASE_MANIFEST,
    FRONTEND_RELEASE_ROOT,
    FRONTEND_RELEASE_SCHEMA_VERSION,
    GPM_PRICE_FRESHNESS_MINUTES,
    LOCAL_NETWORKS,
    PRODUCTION_CANDIDATE_KWH_THRESHOLD,
    _active_frontend_asset_by_path,
    _active_frontend_file,
    _build_forecast_error_metrics,
    _build_live_today_clean_eval,
    _clock_time,
    _column_expr,
    _corrected_daily_energy_values,
    _energy_correction_projection,
    _frontend_path_without_symlinks,
    _get_broad_timestamp_range_for_local_dates,
    _get_config,
    _get_db,
    _get_fixed_total_price,
    _get_local_naive_range_for_local_date,
    _get_sensor_value,
    _get_solar_reader,
    _gpm_local_hour_key,
    _load_active_frontend_release,
    _panel_group_exclusion_reason,
    _safe_frontend_relative_path,
    _stable_api_error,
    _stats_visible_exclusion_reason,
    _table_columns,
    _time_context,
    _verified_frontend_payload,
    local_only,
    local_or_cloud_shell_only,
    local_or_trusted_cloud_only,
    strict_local_only,
)
from .frontend_views import (
    HealthCheckView,
    DashboardView,
    FrontendReleaseManifestView,
    NewStaticFilesView,
)
from .solar_views import (
    SolarDataView,
    PriceDataView,
    SummaryDataView,
    RealtimeDataView,
    EnergyFlowView,
    StatisticsView,
    AnnualForecastView,
)
from .billing_views import (
    BillingDataView,
    GPMPricesView,
    MonthlyTariffsView,
    MonthlyTariffDetailView,
    EnergyMonthlyPriceCorrectionView,
    EnergyMonthlyPricePreviewView,
    MonthlyTariffFinalizeView,
    MonthlyTariffsExportView,
    MonthlyTariffsDefaultsView,
)
from .consumer_views import (
    ConsumerAtlasView,
    ConsumerAtlasBackfillView,
    ConsumerAtlasItemView,
    ConsumerDetailView,
    AmortizationView,
)
from .weather_views import (
    WeatherHistoryView,
    WeatherComparisonView,
    ForecastComparisonView,
    PowerSourcesHistoryView,
    EnergySourcesDailyStatsView,
    WeatherDashboardView,
)
from .settings_views import (
    HybridForecastSettingsPostView,
    HybridForecastStateView,
    SettingsDashboardView,
)
from .smart_charging_dashboard import SmartChargingDashboardView
from .shadow_views import ShadowFingerprintView, ShadowAnalyticsView, ShadowMovementView
from .export_views import AIStatusView, CSVExportView, TypedCSVExportView

_LOGGER = logging.getLogger(__name__)


async def async_setup_views(hass: HomeAssistant) -> None:
    """Register all API views. @zara"""
    api_context = APIContext.initialize(hass)
    context.HASS = hass
    config_path = Path(hass.config.path())
    context.SOLAR_PATH = config_path / "solar_forecast_ml"
    context.GRID_PATH = config_path / "grid_price_monitor"
    _LOGGER.debug(
        "SFML Stats paths: Solar=%s, Grid=%s",
        api_context.solar_path,
        api_context.grid_path,
    )
    hass.http.register_view(HealthCheckView())
    hass.http.register_view(DashboardView())
    hass.http.register_view(FrontendReleaseManifestView())
    hass.http.register_view(SolarDataView())
    hass.http.register_view(PriceDataView())
    hass.http.register_view(SummaryDataView())
    hass.http.register_view(RealtimeDataView())
    hass.http.register_view(NewStaticFilesView())
    hass.http.register_view(EnergyFlowView())
    hass.http.register_view(StatisticsView())
    hass.http.register_view(BillingDataView())
    hass.http.register_view(ConsumerAtlasView())
    hass.http.register_view(ConsumerAtlasBackfillView())
    hass.http.register_view(ConsumerAtlasItemView())
    hass.http.register_view(AmortizationView())
    hass.http.register_view(WeatherHistoryView())
    hass.http.register_view(WeatherComparisonView())
    hass.http.register_view(ForecastComparisonView())
    hass.http.register_view(PowerSourcesHistoryView())
    hass.http.register_view(EnergySourcesDailyStatsView())
    hass.http.register_view(GPMPricesView())
    hass.http.register_view(ConsumerDetailView())
    hass.http.register_view(SettingsDashboardView())
    hass.http.register_view(HybridForecastStateView())
    hass.http.register_view(HybridForecastSettingsPostView(hass))
    hass.http.register_view(SmartChargingDashboardView())
    hass.http.register_view(WeatherDashboardView())
    hass.http.register_view(ShadowFingerprintView())
    hass.http.register_view(MonthlyTariffsView())
    hass.http.register_view(MonthlyTariffDetailView())
    hass.http.register_view(EnergyMonthlyPriceCorrectionView())
    hass.http.register_view(EnergyMonthlyPricePreviewView())
    hass.http.register_view(MonthlyTariffFinalizeView())
    hass.http.register_view(MonthlyTariffsExportView())
    hass.http.register_view(MonthlyTariffsDefaultsView())
    hass.http.register_view(ShadowAnalyticsView())
    hass.http.register_view(ShadowMovementView())
    hass.http.register_view(AIStatusView())
    hass.http.register_view(AnnualForecastView())
    hass.http.register_view(CSVExportView())
    hass.http.register_view(TypedCSVExportView())
    from .dev_mode_views import (
        DevModeInstallView as DevModeInstallView,
        DevModeView as DevModeView,
        DevTokenClearView as DevTokenClearView,
        DevTokenView as DevTokenView,
    )
    from .gpm import async_setup_gpm_views as async_setup_gpm_views
    from .smart_charging import (
        async_setup_smart_charging_views as async_setup_smart_charging_views,
    )
    from .modern import async_setup_modern_views as async_setup_modern_views
    from .support import async_setup_support_views as async_setup_support_views

    hass.http.register_view(DevModeView(hass))
    hass.http.register_view(DevModeInstallView(hass))
    hass.http.register_view(DevTokenView(hass))
    hass.http.register_view(DevTokenClearView(hass))
    await async_setup_gpm_views(hass)
    await async_setup_smart_charging_views(hass)
    await async_setup_modern_views(hass)
    await async_setup_support_views(hass)
    _LOGGER.info("SFML Stats API views registered")
