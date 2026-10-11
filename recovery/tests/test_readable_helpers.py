"""Bounded behavior comparisons for readable, whole-module-verified helpers.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
Only selected pure classes/functions run; no integration module is imported.
"""
import calendar
import copy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
import logging
import math
import os
import random
import re
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
import unittest

from pure_reference import classes, functions

AVAILABLE = (os.environ.get('SFML_RECOVERED_DIR') and os.environ.get('SFML_UNPACKER_DIR')
             and sys.version_info[:2] == (3, 14))
LOGGER = logging.getLogger('readable_helper_fixture')
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


class BehaviorComparison(unittest.TestCase):
    def same(self, original, recovered, *args, **kwargs):
        """Compare output, input mutation, and the existing exception behavior."""
        left_args, right_args = copy.deepcopy(args), copy.deepcopy(args)
        left_kwargs, right_kwargs = copy.deepcopy(kwargs), copy.deepcopy(kwargs)
        def outcome(function, args_, kwargs_):
            try:
                return ('result', function(*args_, **kwargs_))
            except (TypeError, ValueError, OverflowError, ZeroDivisionError) as error:
                return ('error', type(error), str(error))
        self.assertEqual(outcome(recovered, right_args, right_kwargs), outcome(original, left_args, left_kwargs))
        self.assertEqual(right_args, left_args)
        self.assertEqual(right_kwargs, left_kwargs)


@unittest.skipUnless(AVAILABLE, 'Set reference/tool directories; use CPython 3.14')
class ReadableNumericBehavior(BehaviorComparison):
    def test_panel_capacity_allocation_and_rounding(self):
        names = ('_read_group_value', '_positive_float', 'get_group_power_wp',
                 'get_group_capacity_kwp', 'normalize_panel_group_capacity_fields',
                 'sync_panel_group_prediction_map', 'normalize_group_predictions_to_total')
        left, right = functions('core/core_panel_group_forecast.py', names, {})
        for group in ({}, {'power_wp': 2592}, {'capacity_kwp': '6.912'},
                      {'peak_power_wp': -5, 'kwp': 3.5}, SimpleNamespace(capacity_wp=3440),
                      {'power_wp': 'bad', 'capacity_kwp': 4}):
            for name in ('get_group_power_wp', 'get_group_capacity_kwp', 'normalize_panel_group_capacity_fields'):
                self.same(left[name], right[name], group)
        layouts = (None, [], [{'name': 'east'}, {'name': 'west'}, {'name': 'south'}],
                   [{'name': 'east', 'power_kwh': 0.3}, {'name': 'west', 'power_kwh': 0.1}],
                   [{'name': 'east', 'power_wp': 2592}, {'name': 'west', 'capacity_kwp': 6.912}],
                   [{'name': 'east', 'power_kwh': -1}, {'name': 'west', 'power_kwh': None}])
        for groups in layouts:
            for target in (-5, 0, 0.001, 0.0047, 1, 1.234, 20):
                self.same(left['normalize_group_predictions_to_total'], right['normalize_group_predictions_to_total'], groups, target)
        for groups in layouts[2:]:
            self.same(left['sync_panel_group_prediction_map'], right['sync_panel_group_prediction_map'], groups)

    def test_solar_geometry_radiation_and_incidence_boundaries(self):
        names = ('_finite', 'kasten_young_airmass', 'clear_sky_dni_meinel_wm2',
                 'horizontal_direct_to_dni', 'beam_iam_martin_ruiz', 'fill_horizontal_radiation',
                 'poa_irradiance_components', 'erbs_diffuse_fraction', 'extraterrestrial_horizontal', 'erbs_split')
        globals_ = dict(math=math, BEAM_MIN_ELEVATION_DEG=3.0, DNI_MAX_WM2=1000.0,
                        SOLAR_CONSTANT_MEINEL_WM2=1353.0, MEINEL_TRANSMITTANCE=0.7,
                        MEINEL_AIRMASS_EXPONENT=0.678, KASTEN_YOUNG_COEFF=0.50572,
                        KASTEN_YOUNG_OFFSET_DEG=6.07995, KASTEN_YOUNG_EXPONENT=-1.6364,
                        HORIZONTAL_DIRECT_FRACTION=0.85, HORIZONTAL_DIFFUSE_FRACTION=0.15)
        left, right = functions('physics/poa_beam.py', names, globals_, defaults={'beam_iam_martin_ruiz': (0.16,)})
        for elevation in (-6.1, -1, 0, 2.999, 3, 10, 45, 90):
            for name in ('kasten_young_airmass', 'clear_sky_dni_meinel_wm2'):
                self.same(left[name], right[name], elevation)
            for radiation in (None, -5, 0, 100, 800, 1600):
                self.same(left['horizontal_direct_to_dni'], right['horizontal_direct_to_dni'], radiation, elevation)
            for day in (1, 80, 172, 365):
                self.same(left['erbs_split'], right['erbs_split'], 700, elevation, day)
        for angle in (None, -1, 0, 45, 89.999, 90, 100):
            for loss in (-1, 0, 0.16, 1):
                self.same(left['beam_iam_martin_ruiz'], right['beam_iam_martin_ruiz'], angle, loss)
        for ghi in (None, -2, 0, 100, 800):
            for direct, diffuse in ((None, None), (600, None), (None, 200), (600, 200)):
                self.same(left['fill_horizontal_radiation'], right['fill_horizontal_radiation'], ghi, direct, diffuse)
                self.same(left['poa_irradiance_components'], right['poa_irradiance_components'], ghi, direct, diffuse, 45)
        for clearness in (-1, 0, 0.22, 0.2201, 0.8, 0.8001, 1.5):
            self.same(left['erbs_diffuse_fraction'], right['erbs_diffuse_fraction'], clearness)

    def test_grid_overlap_cost_rounding_and_meter_policy(self):
        names = ('_finite', '_non_negative', 'split_tolerance_kwh', 'reconcile_grid_split',
                 'energy_cost_ct', 'displayed_grid_import_kwh', 'billed_import_kwh')
        left, right = functions('extra_features/sfml_stats/core/grid_split.py', names,
                                {'Decimal': Decimal, 'ROUND_HALF_UP': ROUND_HALF_UP, 'SPLIT_TOLERANCE_KWH': 0.02},
                                kwdefaults={'reconcile_grid_split': {'tolerance': 0.02}})
        for measured in (None, '', 'bad', -1, 0, 0.1, 5):
            for house, battery in ((0, 0), (2, 1), (5, 6), (-1, -5), ('bad', None)):
                self.same(left['reconcile_grid_split'], right['reconcile_grid_split'], measured, house, battery)
                for configured in (False, True):
                    for name in ('displayed_grid_import_kwh', 'billed_import_kwh'):
                        self.same(left[name], right[name], measured, house, battery, meter_configured=configured)
        for energy in (None, -1, 0, 0.005, 1, 1.111, 'bad'):
            for price in (None, -25, 0, 1, 37.5, 'bad'):
                self.same(left['energy_cost_ct'], right['energy_cost_ct'], energy, price)
        for hours in (None, 'bad', -1, 0, 1, 24):
            self.same(left['split_tolerance_kwh'], right['split_tolerance_kwh'], hours)

    def test_consumer_config_dates_and_summary_mutation(self):
        names = ('_to_float', '_to_int', 'make_consumer_id', 'sanitize_consumer_config',
                 'valid_consumer_config', 'calculate_billing_period', 'iter_backfill_dates',
                 'build_consumer_atlas_summary', 'integrate_power_to_kwh')
        left, right = functions('extra_features/sfml_stats/core/consumer_atlas.py', names,
            {'calendar': calendar, 're': re, 'date': date, 'timedelta': timedelta,
             'DEFAULT_CONSUMER_COLORS': ('#06b6d4', '#22c55e', '#f59e0b', '#ec4899', '#8b5cf6', '#ef4444', '#14b8a6', '#f97316'),
             'VALID_CONSUMER_CATEGORIES': {'other', 'office', 'heating', 'kitchen', 'utility', 'mobility', 'entertainment'}},
            defaults={'_to_float': (0.0,), '_to_int': (0,), 'sanitize_consumer_config': (None,)},
            kwdefaults={'sanitize_consumer_config': {'default_sort_order': 0}, 'iter_backfill_dates': {'max_days': 370},
                        'build_consumer_atlas_summary': {'home_consumption_kwh': 0.0, 'today': None, 'live_power': None}})
        for payload in (None, {}, {'entity_id': 'sensor.garage_fridge'},
                        {'name': 'Garage Fridge!', 'enabled': 'off', 'start_kwh': '12,5', 'color': '#ABCDEF'},
                        {'category': 'invalid', 'icon': 'a!B@', 'sort_order': 'bad', 'start_kwh': -2}):
            self.same(left['sanitize_consumer_config'], right['sanitize_consumer_config'], payload)
        for year in (2024, 2025, 2026):
            for month, day in ((2, 29), (2, 31), (12, 31), (1, 1), ('bad', 0)):
                self.same(left['calculate_billing_period'], right['calculate_billing_period'], date(year, 3, 1), month, day)
        for days in (-1, 0, 1, 30, 400):
            self.same(left['iter_backfill_dates'], right['iter_backfill_dates'], date(2025, 1, 1), date(2026, 10, 11), max_days=days)
        configs = [{'consumer_id': 'fridge', 'name': 'Fridge', 'enabled': True, 'sort_order': 0, 'start_kwh': 5},
                   {'consumer_id': 'pump', 'name': 'Pump', 'enabled': False, 'sort_order': 1}]
        rows = [{'consumer_id': 'fridge', 'date': '2026-10-11', 'kwh': 2, 'peak_w': 150, 'samples': 12, 'last_power_w': 40},
                {'consumer_id': 'fridge', 'date': '2026-10-10', 'kwh': 3, 'peak_w': 130},
                {'consumer_id': 'pump', 'date': '2026-10-11', 'kwh': 4, 'peak_w': 450},
                {'consumer_id': 'unknown', 'date': '2026-10-11', 'kwh': 8}]
        for home in (0, 5, 50):
            for power in (None, {'fridge': 100}):
                self.same(left['build_consumer_atlas_summary'], right['build_consumer_atlas_summary'], configs, rows,
                          today=date(2026, 10, 11), home_consumption_kwh=home, live_power=power)
        for power in (None, -3, 0, 100, '12,5'):
            for seconds in (-1, 0, 30, 3600):
                self.same(left['integrate_power_to_kwh'], right['integrate_power_to_kwh'], power, seconds)

    def test_conservative_planning_caps_and_freshness_boundaries(self):
        names = ('_non_negative_float', '_parse_date', 'parse_p10_timestamp', 'p10_age_hours',
                 'persisted_p10_freshness', 'p10_morning_recompute_allowed', 'round_not_above',
                 'conservative_planning_value', 'cap_persisted_conservative_value',
                 'remaining_hourly_forecast_kwh', 'remaining_today_expected_kwh', 'remaining_conservative_value')
        left, right = functions('sensors/conservative_planning.py', names,
            {'math': math, 'datetime': datetime, 'date': date}, defaults={'p10_age_hours': (None,)},
            kwdefaults={'persisted_p10_freshness': {'now': None, 'max_age_hours': 30.0},
                        'p10_morning_recompute_allowed': {'now': None, 'window_minutes': 45.0},
                        'conservative_planning_value': {'sfml_weight': 0.65, 'tfs_weight': 0.35, 'precision': 4},
                        'cap_persisted_conservative_value': {'precision': 2},
                        'remaining_hourly_forecast_kwh': {'precision': 2},
                        'remaining_today_expected_kwh': {'hourly_values': None, 'expected_day_kwh': None, 'actual_today_kwh': None, 'precision': 2},
                        'remaining_conservative_value': {'precision': 2}})
        now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
        for minutes in (-1, 0, 44.999, 45, 45.001, 1800, 1801):
            updated = now - timedelta(minutes=minutes)
            self.same(left['persisted_p10_freshness'], right['persisted_p10_freshness'], now.date(), updated, now=now)
            self.same(left['p10_morning_recompute_allowed'], right['p10_morning_recompute_allowed'], now.date(), 'morning_routine', updated, now=now)
        for sfml in (None, -1, 0, 0.004, 1.235, 15):
            for p10 in (None, -2, 0, 1.111, 30):
                self.same(left['conservative_planning_value'], right['conservative_planning_value'], sfml, p10)
                self.same(left['cap_persisted_conservative_value'], right['cap_persisted_conservative_value'], p10, sfml)
                self.same(left['remaining_conservative_value'], right['remaining_conservative_value'], sfml, 2, p10)
        for hour in (-1, 0, 12, 23, 24, 'bad'):
            for hourly in (None, {}, {'11:00': 2, '12:30': 3, 23: 5, 'bad': 10}, {'12': None}, {'12': -2}):
                self.same(left['remaining_today_expected_kwh'], right['remaining_today_expected_kwh'], current_hour=hour,
                          hourly_values=hourly, expected_day_kwh=20, actual_today_kwh=7)

    def test_weather_uplift_requires_confirmation(self):
        left, right = functions('production/production_rest_of_day_uplift.py',
            ('cap_rest_of_day_uplift', 'rest_of_day_uplift_from_weather_evidence'),
            {'math': math, 'REST_OF_DAY_UPLIFT_MAX': 1.15, 'GHI_CONFIRM_MIN_RATIO': 1.15})
        for uplift in (None, 'bad', -1, 0, 1, 1.05, 1.15, 10):
            self.same(left['cap_rest_of_day_uplift'], right['cap_rest_of_day_uplift'], uplift)
            for local in (False, True):
                for actual, forecast in ((None, None), ('bad', 20), (0, 0), (114.9, 100), (115, 100), (400, 100)):
                    self.same(left['rest_of_day_uplift_from_weather_evidence'], right['rest_of_day_uplift_from_weather_evidence'],
                              uplift, has_local_ghi_sensor=local, actual_ghi_wm2=actual, forecast_ghi_wm2=forecast)

    def test_weather_contract_only_accepts_bounded_values(self):
        relative = 'extra_features/solar_forecast_eai/weather_contract.py'
        source, _ = classes(relative, 'WeatherSource', {'StrEnum': StrEnum}, (StrEnum,))
        status, _ = classes(relative, 'WeatherProviderStatus', {'StrEnum': StrEnum}, (StrEnum,))
        source_values = frozenset(member.value for member in source)
        status_values = frozenset(member.value for member in status)
        left, right = functions(relative, ('normalize_weather_source', 'normalize_provider_status'),
                                {'_SOURCE_VALUES': source_values, '_STATUS_VALUES': status_values})
        for value in (*source_values, *status_values, None, 1, {}, 'untrusted-provider', b'unknown', 'OK'):
            for name in ('normalize_weather_source', 'normalize_provider_status'):
                self.same(left[name], right[name], value)


@unittest.skipUnless(AVAILABLE, 'Set reference/tool directories; use CPython 3.14')
class ReadableClassBehavior(BehaviorComparison):
    @classmethod
    def setUpClass(cls):
        cls.feature_classes = classes('extra_features/solar_forecast_eai/ai/ai_feature_engineering.py', 'FeatureEngineer',
                                      {'Any': Any, 'Dict': Dict, 'List': List, 'Optional': Optional, '_LOGGER': LOGGER})
        cls.price_classes = classes('extra_features/grid_price_monitor/core/calculator.py', 'PriceCalculator',
                                    {'Any': Any, '_LOGGER': LOGGER})

    def test_feature_vectors_seasons_clamps_and_thermal_mass(self):
        left, right = (cls() for cls in self.feature_classes)
        for thermal in (-1, 0, 0.5, 1, 5):
            left.update_thermal_mass(thermal)
            right.update_thermal_mass(thermal)
            for day in (1, 59, 60, 151, 152, 243, 244, 334, 335, 365):
                for temp in (-30, 5, 45):
                    for dhw in (False, True):
                        self.same(left.extract_features, right.extract_features, 12, day,
                                  {'temperature': temp, 'wind_speed': 12, 'humidity': 110},
                                  {'consumption_yesterday': 150, 'consumption_same_hour': -1},
                                  is_dhw_mode=dhw, temp_history_3h=[5, 3, 1])
        self.assertEqual(right.get_feature_count(), left.get_feature_count())

    def test_record_aliases_invalid_values_and_hour_sequences(self):
        left, right = (cls() for cls in self.feature_classes)
        records = (None, [], {}, {'weather': 'bad'}, {'weather': {'temp': None, 'temperature_c': -5, 'wind': 3}},
                   {'weather': {'temperature': 'bad'}}, {'target_hour': 'bad'},
                   {'weather_corrected': {'temperature': 10}, 'weather': {'temperature': -20},
                    'production_yesterday': 20, 'production_same_hour_yesterday': 3})
        for record in records:
            self.same(left.extract_record_features, right.extract_record_features, record)
        generator = random.Random(3482)
        rows = [{'hour': hour, 'day_of_year': 284, 'weather': {'temperature': generator.uniform(-10, 15)},
                 'historical': {'consumption_yesterday': 25, 'consumption_same_hour': 1}, 'is_dhw_mode': hour % 3 == 0}
                for hour in range(24)]
        for count in (0, 1, 3, 4, 24):
            self.same(left.extract_sequence, right.extract_sequence, rows[:count])

    def test_price_config_calibration_trends_and_forecast_entries(self):
        for vat in (0, 5, 19):
            left, right = (cls(vat, 12, 2, 1) for cls in self.price_classes)
            for price in (-20, 0, 0.005, 5, 20, 100):
                for name in ('calculate_gross_spot', 'calculate_total_price', 'calculate_spot_from_total'):
                    self.same(getattr(left, name), getattr(right, name), price)
                self.same(left.build_forecast_entry, right.build_forecast_entry, {'price': price, 'hour': 12}, 35)
                self.same(left.calculate_markup_from_calibration, right.calculate_markup_from_calibration, price, 10)
            for current in (None, 0, 10):
                for following in (None, -1.01, 0, 1, 1.01, 10):
                    self.same(left.calculate_trend, right.calculate_trend, current, following)
            left.update_config(vat_rate=0, grid_fee=0, taxes_fees=5, provider_markup=0)
            right.update_config(vat_rate=0, grid_fee=0, taxes_fees=5, provider_markup=0)
            self.assertEqual(right.get_config(), left.get_config())
            for property_name, value in (('vat_rate', 7), ('grid_fee', 3), ('taxes_fees', 4), ('provider_markup', 2)):
                setattr(left, property_name, value)
                setattr(right, property_name, value)
            self.assertEqual(right.get_config(), left.get_config())
