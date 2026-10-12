"""Paired threshold and heat-pump source checks with owned dependencies.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No integration or real Home Assistant registry is imported.
"""
import copy
from statistics import fmean
from types import SimpleNamespace
import unittest

from pure_reference import functions


class ModuleBindingCheckpointTests(unittest.TestCase):
    def test_threshold_modes_validation_and_current_slot_decisions(self):
        module = 'extra_features/grid_price_monitor/core/thresholds.py'
        limits = {'max_price': {'min': 0, 'max': 100, 'step': 0.5},
                  'force_charge_price': {'min': 0, 'max': 100, 'step': 0.5},
                  'below_average_pct': {'min': 0, 'max': 50, 'step': 1},
                  'cheapest_hours': {'min': 1, 'max': 12, 'step': 1}}
        names = ('thresholds_payload', 'threshold_options', '_as_number', '_in_limit',
                 'merge_and_validate_thresholds', 'evaluate_day', 'slot_is_cheap', 'evaluate_current')
        pairs = functions(module, names, {
            'THRESHOLD_LIMITS': limits, 'THRESHOLD_MODES': ('absolute', 'below_average', 'cheapest_hours'),
            'THRESHOLD_MODE_ABSOLUTE': 'absolute', 'THRESHOLD_MODE_BELOW_AVERAGE': 'below_average',
            'THRESHOLD_MODE_CHEAPEST_HOURS': 'cheapest_hours', 'ThresholdConfig': SimpleNamespace,
            'DayThresholds': SimpleNamespace, 'fmean': fmean})
        current = SimpleNamespace(mode='absolute', max_price=20, force_charge_price=15,
                                  below_average_pct=10, cheapest_hours=2)
        changes = [{}, {'unknown': 1}, {'mode': 'invalid'}, {'max_price': 10},
                   {'max_price': 100}, {'max_price': -1}, {'max_price': True},
                   {'max_price': float('nan')}, {'threshold_mode': 'below_average'},
                   {'below_average_pct': 51}, {'force_charge_price': 0},
                   {'cheapest_hours': 1}, {'cheapest_hours': 3.5},
                   {'cheapest_hours': float('nan')}, {'cheapest_hours': float('inf')}]
        for change in changes:
            outputs = []
            for namespace in pairs:
                try:
                    config, errors = namespace['merge_and_validate_thresholds'](current, change)
                    result = (vars(config) if config else None, errors)
                except (ValueError, OverflowError) as error:
                    result = (type(error).__name__, str(error))
                outputs.append(result)
            self.assertEqual(outputs[1], outputs[0])
        slot_lists = [[], [(0, 0)], [(3, -5), (1, 10), (2, 10), (0, 30)],
                      [(2, 5), (2, 5), (1, 5)]]
        for mode in ('absolute', 'below_average', 'cheapest_hours'):
            config = SimpleNamespace(**dict(vars(current), mode=mode))
            for slots in slot_lists:
                outcomes = []
                for namespace in pairs:
                    day = namespace['evaluate_day'](slots, config)
                    decisions = [namespace['evaluate_current'](price, hour, day, config)
                                 for hour in (0, 1, 2, 3) for price in (None, -5, 0, 15, 20, 30)]
                    outcomes.append((vars(day), decisions, namespace['thresholds_payload'](config),
                                     namespace['threshold_options'](config)))
                self.assertEqual(outcomes[1], outcomes[0])

    def test_eai_mapping_fallback_units_and_unavailable_setup_status(self):
        module = 'extra_features/sfml_stats/core/eai_wp_sources.py'
        power_units = {'w', 'kw', 'mw', 'watt', 'watts', 'kilowatt', 'megawatt', 'kilowatts', 'megawatts'}
        mapping = {'stats_power': 'wp_power_entity', 'stats_daily': 'wp_energy_today', 'stats_unmapped': None}
        schema = ({'key': 'wp_power_entity', 'tier': 'owned', 'label': 'Power', 'effect': 'owned', 'unit_class': 'power'},
                  {'key': 'wp_energy_today', 'tier': 'owned', 'label': 'Energy', 'effect': 'owned', 'unit_class': 'energy'})
        pairs = functions(module, ('eai_config', '_clean_entity', '_state', '_unit_text', 'power_unit_accepted',
                                  '_unit_status', 'resolved_heatpump_entity', 'mapping_config', 'heatpump_setup_status'), {
            'EAI_DOMAIN': 'solar_forecast_eai', 'STATS_TO_EAI': mapping,
            'CONF_SENSOR_HEATPUMP_POWER': 'stats_power', 'POWER_UNITS': power_units,
            '_UNIT_SETS': {'power': power_units, 'energy': {'wh', 'kwh', 'mwh'}},
            'EAI_HEATPUMP_SENSORS': schema, 'EAI_INTEGRATION_PATH': 'owned-path', 'EAI_SENSOR_MENU': 'owned-menu'})
        class States:
            def __init__(self, power_state, power_unit):
                self.values = {'sensor.eai_power': SimpleNamespace(state=power_state,
                    attributes={'unit_of_measurement': power_unit}),
                    'sensor.eai_energy': SimpleNamespace(state='5', attributes={'unit_of_measurement': 'kWh'})}
            def get(self, entity_id):
                return self.values.get(entity_id)
        stats = {'stats_power': 'sensor.stats_power', 'stats_daily': 'sensor.stats_energy',
                 'stats_unmapped': 'sensor.stats_unmapped'}
        scenarios = [(True, '100', 'W'), (True, '0', ' kW '), (True, '100', 'V'),
                     (True, 'unavailable', 'W'), (True, 'unknown', 'W'),
                     (True, '', 'W'), (False, '100', 'W')]
        for installed, power_state, unit in scenarios:
            outputs = []
            for namespace in pairs:
                entry = SimpleNamespace(data={'wp_power_entity': ' sensor.eai_power ',
                    'wp_energy_today': 'sensor.old_energy', 'heat_pump_enabled': True},
                    options={'wp_energy_today': 'sensor.eai_energy'})
                hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda domain: [entry] if installed else []),
                    states=States(power_state, unit))
                result = namespace['mapping_config'](hass, copy.deepcopy(stats))
                setup = namespace['heatpump_setup_status'](hass)
                outputs.append((namespace['eai_config'](hass), result, setup))
            self.assertEqual(outputs[1], outputs[0])
            self.assertEqual(stats['stats_power'], 'sensor.stats_power')
            if installed and unit == 'V':
                self.assertEqual(outputs[1][1]['stats_power'], 'sensor.stats_power')
                self.assertEqual(outputs[1][2]['sensors'][0]['status'], 'wrong_unit')
            if installed and power_state in ('unavailable', 'unknown', ''):
                self.assertEqual(outputs[1][2]['sensors'][0]['status'], 'unavailable')
        for namespace in pairs:
            self.assertEqual(namespace['eai_config'](None), {})
            self.assertIsNone(namespace['_state'](None, 'sensor.owned'))


if __name__ == '__main__':
    unittest.main()
