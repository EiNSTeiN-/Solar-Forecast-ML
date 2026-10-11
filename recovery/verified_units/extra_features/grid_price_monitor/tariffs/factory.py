# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Build the active PriceSource from config and entitlements."""
from __future__ import annotations
from typing import Any
from ..const import CONF_COUNTRY, CONF_CSV_COMMUNITY, CONF_FIXED, CONF_GRID_FEE, CONF_PROVIDER_MARKUP, CONF_TARIFF_MODE, CONF_TAXES_FEES, CONF_TIME_OF_USE, CONF_TIME_WINDOWS, CONF_VAT_RATE, DEFAULT_COUNTRY, DEFAULT_GRID_FEE, DEFAULT_PROVIDER_MARKUP, DEFAULT_TAXES_FEES, TARIFF_MODE_CSV_COMMUNITY, TARIFF_MODE_DEMO, TARIFF_MODE_DYNAMIC, TARIFF_MODE_FIXED, TARIFF_MODE_TIME_OF_USE, TARIFF_MODE_TIME_WINDOWS, VAT_RATE_AT, VAT_RATE_DE
from ..core.calculator import PriceCalculator
from ..core.price_service import ElectricityPriceService
from ..license.entitlements import has_dynamic_tariff, has_tariff_models, is_demo_entitlements
from .awattar import AwattarSource
from .base import PriceSource
from .csv_community import CsvCommunitySource
from .demo import DemoSource
from .fixed import FixedSource
from .time_of_use import TimeOfUseSource
from .time_windows import TimeWindowSource

def _country(config: dict[str, Any]) -> str:
    return str(config.get(CONF_COUNTRY, DEFAULT_COUNTRY) or DEFAULT_COUNTRY)

def _calculator(config: dict[str, Any]) -> PriceCalculator:
    _var_var_0 = _country(config)
    _var_var_1 = VAT_RATE_AT if _var_var_0 == 'AT' else VAT_RATE_DE
    return PriceCalculator(vat_rate=int(config.get(CONF_VAT_RATE, _var_var_1) or _var_var_1), grid_fee=float(config.get(CONF_GRID_FEE, DEFAULT_GRID_FEE)), taxes_fees=float(config.get(CONF_TAXES_FEES, DEFAULT_TAXES_FEES)), provider_markup=float(config.get(CONF_PROVIDER_MARKUP, DEFAULT_PROVIDER_MARKUP)))

def _fixed_source(config: dict[str, Any]) -> FixedSource:
    _var_var_2 = config.get(CONF_FIXED) or {}
    _var_var_3 = _var_var_2.get('total_price', config.get('fixed_total_price', 0))
    return FixedSource(float(_var_var_3 or 0))

def _tou_source(config: dict[str, Any]) -> TimeOfUseSource:
    _var_var_2 = config.get(CONF_TIME_OF_USE) or {}
    return TimeOfUseSource(high_price=float(_var_var_2.get('high_price', 0) or 0), low_price=float(_var_var_2.get('low_price', 0) or 0), high_start=_var_var_2.get('high_start', '06:00:00'), high_end=_var_var_2.get('high_end', '22:00:00'), weekend_low=bool(_var_var_2.get('weekend_low', True)), holiday_low=bool(_var_var_2.get('holiday_low', False)), country=_country(config))

def _windows_source(config: dict[str, Any]) -> TimeWindowSource:
    _var_var_2 = config.get(CONF_TIME_WINDOWS) or {}
    return TimeWindowSource(default_price=float(_var_var_2.get('default_price', 0) or 0), windows=list(_var_var_2.get('windows') or []))

def _dynamic_source(config: dict[str, Any], price_service: ElectricityPriceService | None) -> AwattarSource:
    return AwattarSource(_country(config), _calculator(config), price_service=price_service)

def _build_mode(mode: str, config: dict[str, Any], price_service: ElectricityPriceService | None) -> PriceSource:
    if mode == TARIFF_MODE_FIXED:
        return _fixed_source(config)
    if mode == TARIFF_MODE_TIME_OF_USE:
        return _tou_source(config)
    if mode == TARIFF_MODE_TIME_WINDOWS:
        return _windows_source(config)
    return _dynamic_source(config, price_service)

def build_price_source(config: dict[str, Any], entitlements: frozenset[str], price_service: ElectricityPriceService | None=None) -> PriceSource:
    if is_demo_entitlements(entitlements):
        return DemoSource()
    _var_var_4 = str(config.get(CONF_TARIFF_MODE, TARIFF_MODE_DYNAMIC) or TARIFF_MODE_DYNAMIC)
    if _var_var_4 == TARIFF_MODE_DEMO:
        return DemoSource()
    if _var_var_4 != TARIFF_MODE_DYNAMIC and (not has_tariff_models(entitlements)):
        if has_dynamic_tariff(entitlements):
            _var_var_4 = TARIFF_MODE_DYNAMIC
        else:
            return DemoSource()
    if _var_var_4 == TARIFF_MODE_CSV_COMMUNITY:
        _var_var_5 = config.get(CONF_CSV_COMMUNITY) or {}
        _var_var_6 = str(_var_var_5.get('base_mode') or TARIFF_MODE_DYNAMIC)
        if _var_var_6 == TARIFF_MODE_CSV_COMMUNITY:
            _var_var_6 = TARIFF_MODE_DYNAMIC
        _var_var_7 = _build_mode(_var_var_6, config, price_service)
        return CsvCommunitySource(_var_var_7)
    return _build_mode(_var_var_4, config, price_service)
