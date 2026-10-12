"""Selected behavior checks for newly readable shared-return module trees.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
All services, meters and capability states are controlled owned fixtures.
"""
import asyncio
from collections.abc import Mapping
import copy
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
import unittest

from pure_reference import classes, functions
from test_isolated_initialization import logger_for


class SharedReturnCheckpointTests(unittest.TestCase):
    def test_time_window_boundaries_overlap_and_first_match_prices(self):
        module = 'extra_features/grid_price_monitor/tariffs/time_windows.py'
        pairs = functions(module, ('_clock_minutes', 'window_covers', 'windows_overlap'),
                          {'parse_clock': time.fromisoformat})
        fixtures = [[{'start': '22:00', 'end': '02:00', 'weekdays': [4], 'price': 10}],
                    [{'start': '22:00', 'end': '02:00', 'weekdays': [4], 'price': 10},
                     {'start': '01:00', 'end': '03:00', 'weekdays': [5], 'price': 20}],
                    [{'start': '00:00', 'end': '00:00', 'weekdays': [6], 'price': 7}]]
        for windows in fixtures:
            self.assertEqual(pairs[1]['windows_overlap'](windows), pairs[0]['windows_overlap'](windows))
            for day_offset in range(7):
                for hour in (0, 1, 2, 3, 21, 22, 23):
                    local = datetime(2026, 10, 5) + timedelta(days=day_offset, hours=hour)
                    for window in windows:
                        self.assertEqual(pairs[1]['window_covers'](local, window),
                                         pairs[0]['window_covers'](local, window))
        self.assertTrue(pairs[1]['windows_overlap'](fixtures[1]))
        self.assertFalse(pairs[1]['windows_overlap'](fixtures[0]))
        original, recovered = classes(module, 'TimeWindowSource', {
            'TARIFF_MODE_TIME_WINDOWS': 'time_windows',
            'window_covers': pairs[0]['window_covers']})
        for windows in fixtures:
            left = original(default_price=5, windows=copy.deepcopy(windows))
            right = recovered(default_price=5, windows=copy.deepcopy(windows))
            self.assertEqual(left.label, right.label)
            for day_offset in range(7):
                for hour in (0, 1, 2, 22, 23):
                    local = datetime(2026, 10, 5) + timedelta(days=day_offset, hours=hour)
                    self.assertEqual(left.price_for(local), right.price_for(local))

    def test_battery_integrator_gap_limits_unavailable_inputs_and_resets(self):
        module = 'extra_features/sfml_stats/core/battery_tracker.py'
        pairs = functions(module, ('_cap_riemann_delta_hours',),
            {'RIEMANN_MAX_GAP_HOURS': 0.5, '_RIEMANN_GAP_WARNING_GRACE_HOURS': 0.25})
        for hours in (-1, 0, 0.49, 0.5, 0.75, 0.751, 3):
            self.assertEqual(pairs[0]['_cap_riemann_delta_hours'](hours),
                             pairs[1]['_cap_riemann_delta_hours'](hours))
        outputs = []
        for index in (0, 1):
            events = []
            selected = classes(module, 'BatteryTracker', {
                'callback': lambda callback: callback, '_LOGGER': logger_for(events),
                'ha_now': lambda hass: hass.now, 'as_ha_local': lambda value, hass: value,
                'RIEMANN_MAX_GAP_HOURS': 0.5,
                '_cap_riemann_delta_hours': pairs[index]['_cap_riemann_delta_hours'],
            })[index]
            hass = SimpleNamespace(now=datetime(2026, 10, 11))
            tracker = selected(hass, 'owned-entry')
            tracker._schedule_save = lambda: events.append(('save',))
            readings = [(0, '1000'), (0.5, '2000'), (2, 'unavailable'),
                        (3, '500'), (3.5, 'invalid'), (4, '-100'), (25, '300')]
            snapshots = []
            for hours, state in readings:
                hass.now = datetime(2026, 10, 11) + timedelta(hours=hours)
                tracker._async_state_changed(SimpleNamespace(data={'new_state': SimpleNamespace(state=state)}))
                snapshots.append(tracker.get_statistics())
            outputs.append((snapshots, events))
        self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(outputs[1][0][3]['battery_charged_today'], 1.5)

    def test_invoice_validation_retains_codes_numeric_rules_and_text(self):
        module = 'extra_features/sfml_stats/core/invoice_archive.py'
        error_class = classes(module, 'InvoiceArchiveError', {}, (ValueError,))[0]
        names = ('_parse_date', '_required_positive', '_optional_non_negative', '_optional_text', 'validate_invoice')
        pairs = functions(module, names, {'InvoiceArchiveError': error_class,
            'datetime': datetime, 'date': date, 'Mapping': Mapping, '_DATE_FORMAT': '%Y-%m-%d',
            '_OPTIONAL_NUMBERS': ('base_fee_eur', 'net_energy_price_ct', 'solar_kwh', 'feed_in_eur', 'savings_eur')})
        valid = {'period_from': '2026-09-01', 'period_to': '2026-09-30',
                 'grid_kwh': '120', 'total_eur': '35.5', 'provider': ' Owned ', 'base_fee_eur': '0'}
        cases = [valid, dict(valid, id='7'), dict(valid, provider=None, note='  '), None,
                 dict(valid, period_from='2026-9-01'), dict(valid, period_to='2026-08-31'),
                 dict(valid, grid_kwh=0), dict(valid, total_eur=float('nan')),
                 dict(valid, id=0), dict(valid, base_fee_eur=-1), dict(valid, note=7)]
        for payload in cases:
            outputs = []
            for namespace in pairs:
                try:
                    result = namespace['validate_invoice'](copy.deepcopy(payload))
                except error_class as error:
                    result = (error.code, str(error), type(error.__cause__).__name__)
                outputs.append(result)
            self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(pairs[1]['validate_invoice'](valid)['provider'], 'Owned')

    def test_forecast_regeneration_dependency_calls_and_fallbacks(self):
        module = 'production/production_forecast_regeneration.py'
        class OwnedGenerationError(Exception):
            pass
        for scenario in ('normal', 'no_pipeline', 'refresh_false', 'refresh_error',
                         'no_weather', 'empty_weather', 'no_orchestrator', 'generation_error'):
            outputs = []
            for index in (0, 1):
                events = []
                class Pipeline:
                    weather_corrector = None
                    async def update_weather_cache(self, *, force):
                        events.append(('refresh', force))
                        if scenario == 'refresh_error':
                            raise RuntimeError('owned refresh failure')
                        return scenario != 'refresh_false'
                class Weather:
                    async def get_corrected_hourly_forecast(self, **options):
                        events.append(('weather', options))
                        return [] if scenario == 'empty_weather' else [{'date': '2026-10-11', 'hour': 12, 'ghi': 100}]
                class Orchestrator:
                    async def orchestrate_forecast(self, **options):
                        events.append(('forecast', copy.deepcopy(options)))
                        if scenario == 'generation_error':
                            raise OwnedGenerationError('owned generation failure')
                        return {'hourly': [{'hour': 12, 'prediction_kwh': 1.5}]}
                coordinator = SimpleNamespace(
                    weather_pipeline_manager=None if scenario == 'no_pipeline' else Pipeline(),
                    weather_service=None if scenario == 'no_weather' else Weather(),
                    sensor_collector=SimpleNamespace(collect_all_sensor_data_dict=lambda: {'owned': 3}),
                    learned_correction_factor=1.1)
                if scenario != 'no_orchestrator':
                    coordinator.forecast_orchestrator = Orchestrator()
                cls = classes(module, 'ForecastRegenerationHelper',
                    {'_LOGGER': logger_for(events), 'ForecastGenerationError': OwnedGenerationError})[index]
                helper = cls(None, coordinator, None)
                async def run():
                    await helper.refresh_weather_data(log_prefix='Owned refresh')
                    return await helper.generate_full_forecast('2026-10-11', log_prefix='Owned generate')
                outputs.append((asyncio.run(run()), events))
            self.assertEqual(outputs[1], outputs[0], scenario)

    def test_capability_provider_retains_invalid_and_expired_license_gates(self):
        module = 'extra_features/solar_forecast_eai/capability.py'
        entitlements = frozenset(('eai_enabled', 'forecast_enabled', 'building_analysis_enabled', 'advanced_diagnostics_enabled'))
        features = {'heat_pump_enabled': True, 'wallbox_enabled': False, 'heating_enabled': False,
                    'weather_intelligence_enabled': False, 'grid_price_enabled': True}
        feature_function = functions(module, ('_features_from_config',), {})[0]['_features_from_config']
        now = datetime(2026, 10, 11, tzinfo=timezone.utc)
        for status in ('invalid', 'valid', 'expired'):
            outputs = []
            for index in (0, 1):
                events = []
                def state():
                    return SimpleNamespace(installed=True, license_status='not_provided', license_id=None,
                        license_expires_at=None, entitlements=frozenset(), configured=False,
                        configuration_status='not_configured', onboarding_state='license_required',
                        capability_level='preview', data_mode='mock', data_freshness=None, features=dict(features))
                def replace(state, **values):
                    return SimpleNamespace(**dict(vars(state), **values))
                class Engine:
                    def __init__(self, hass):
                        pass
                    def configure(self, config, allowed):
                        events.append(('configure', copy.deepcopy(config), allowed))
                    def snapshot(self):
                        events.append(('snapshot',))
                        return {'owned': 1}, now
                cls = classes(module, 'EAICapabilityProvider', {
                    'EAICapabilityState': state, 'EAIInsightsEngine': Engine, 'replace': replace,
                    '_features_from_config': feature_function, 'FULL_PACKAGE_ENTITLEMENTS': entitlements,
                    '_DEFAULT_FEATURES': features, 'datetime': datetime, 'timezone': timezone})[index]
                provider = cls('owned-hass')
                provider.update_license(SimpleNamespace(status=SimpleNamespace(value='valid' if status != 'invalid' else 'invalid'),
                    payload=SimpleNamespace(license_id='owned', expires_at=now - timedelta(days=1) if status == 'expired' else None),
                    entitlements=frozenset(('eai_enabled',))))
                provider.update_configuration(configured=True, capability_level='owned', config={'wallbox_enabled': True})
                snapshot = provider.ems_snapshot(now=now)
                outputs.append((snapshot, vars(provider._state), events))
            self.assertEqual(outputs[1], outputs[0], status)
            if status != 'valid':
                self.assertIsNone(outputs[1][0])
            if status == 'expired':
                self.assertEqual(outputs[1][1]['license_status'], 'validation_error')


if __name__ == '__main__':
    unittest.main()
