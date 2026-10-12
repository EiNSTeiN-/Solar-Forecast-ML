"""Paired live-value and panel-group checks with owned registry/physics stand-ins.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No Home Assistant registry, inverter, database or physics integration is loaded.
"""
import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
import unittest

from pure_reference import classes
from test_isolated_initialization import logger_for


class CollectorAndGroupsTests(unittest.TestCase):
    def test_sensor_values_availability_configuration_and_startup_wait(self):
        for state in ('12,5 C', '0', '-3.2', 'invalid', 'unknown', 'unavailable', '', None):
            outputs = []
            for index in (0, 1):
                events = []
                values = {'sensor.temperature': SimpleNamespace(state=state),
                          'sensor.power': SimpleNamespace(state='25 W')}
                async def sleep(seconds):
                    events.append(('sleep', seconds))
                cls = classes('sensors/sensor_data_collector.py', 'SensorDataCollector', {
                    'EXTERNAL_SENSOR_MAPPING': {'temperature': 'temperature_entity',
                        'current_power': 'power_entity', 'solar_radiation': 'solar_entity'},
                    '_LOGGER': logger_for(events), 'asyncio': SimpleNamespace(sleep=sleep)})[index]
                collector = cls(SimpleNamespace(states=SimpleNamespace(get=values.get)),
                    SimpleNamespace(data={'temperature_entity': ' sensor.temperature ',
                                          'power_entity': 'sensor.power', 'solar_entity': ' '}))
                outputs.append((collector.collect_all_sensor_data_dict(), collector.get_available_sensors(),
                    collector.count_available_sensors(), collector.get_weather_data(), collector.get_power_data(),
                    collector.validate_sensor_config(), asyncio.run(collector.wait_for_external_sensors(max_wait=3)), events))
            self.assertEqual(outputs[1], outputs[0])
            self.assertEqual(outputs[1][6], 1 if state in ('invalid', 'unknown', 'unavailable', '', None) else 2)
        for mode in ('none_configured', 'timeout', 'becomes_available'):
            outputs = []
            for index in (0, 1):
                events = []; state = SimpleNamespace(state='unavailable')
                async def sleep(seconds):
                    events.append(('sleep', seconds))
                    if mode == 'becomes_available':
                        state.state = '15'
                cls = classes('sensors/sensor_data_collector.py', 'SensorDataCollector', {
                    'EXTERNAL_SENSOR_MAPPING': {'temperature': 'temperature_entity'},
                    '_LOGGER': logger_for(events), 'asyncio': SimpleNamespace(sleep=sleep)})[index]
                collector = cls(SimpleNamespace(states=SimpleNamespace(get=lambda entity: state)),
                               SimpleNamespace(data={} if mode == 'none_configured' else {'temperature_entity': 'sensor.owned'}))
                result = asyncio.run(collector.wait_for_external_sensors(max_wait=3))
                outputs.append((result, events))
            self.assertEqual(outputs[1], outputs[0])
            self.assertEqual(outputs[1][0], 1 if mode == 'becomes_available' else 0)

    def test_panel_groups_capacity_calibration_snow_and_contributions(self):
        @dataclass
        class Group:
            name: str
            power_wp: float
            azimuth_deg: float
            tilt_deg: float
            power_sensor: str | None = None
            @property
            def power_kwp(self):
                return self.power_wp / 1000
            @property
            def geometry(self):
                return (self.tilt_deg, self.azimuth_deg)
        for enabled, snow_by_group in ((True, {}), (True, {'second': True}), (False, {'first': True})):
            outputs = []
            for index in (0, 1):
                events = []
                class Engine:
                    def __init__(self, **options):
                        self.capacity = options['system_capacity_kwp']; events.append(('engine', options))
                    def calculate_poa_irradiance(self, irradiance, sun, geometry):
                        events.append(('poa', geometry)); return SimpleNamespace(poa_total=100, aoi_deg=30)
                    def calculate_power_output(self, irradiance, sun, temperature, geometry):
                        events.append(('power', temperature, geometry)); return SimpleNamespace(power_kwh=self.capacity * 0.2)
                class Calibrator:
                    def get_correction_factor(self, **options):
                        events.append(('calibration', options)); return 0.5 if options['snow_covered_panels'] else 1.1
                cls = classes('physics/panel_group_calculator.py', 'PanelGroupCalculator', {
                    'PanelGroup': Group, 'PhysicsEngine': Engine,
                    'PanelGroupResult': lambda **values: SimpleNamespace(**dict(contribution_percent=0, **values)),
                    'MultiGroupResult': SimpleNamespace, '_LOGGER': logger_for(events)})[index]
                calculator = cls([{'name': 'first', 'power_wp': 2000, 'azimuth': 90, 'tilt': 30},
                                  Group('second', 1000, 180, 40)])
                calculator.set_calibrator(Calibrator()); calculator._calibration_enabled = enabled
                result = calculator.calculate_total_power(SimpleNamespace(ghi=100), SimpleNamespace(elevation_deg=45),
                    ambient_temp_c=15, hour=12, cloud_cover=20, snow_per_group=snow_by_group)
                outputs.append((calculator.group_count, calculator.total_capacity_kwp, result.total_power_kwh,
                    [(value.group.name, value.power_kwh, value.calibration_factor, value.contribution_percent)
                     for value in result.group_results], events))
            self.assertEqual(outputs[1], outputs[0])
            self.assertEqual(outputs[1][0:2], (2, 3.0))
            self.assertAlmostEqual(sum(value[3] for value in outputs[1][3]), 100)
        outputs = []
        for index in (0, 1):
            events = []
            cls = classes('physics/panel_group_calculator.py', 'PanelGroupCalculator',
                          {'PanelGroup': Group, 'MultiGroupResult': SimpleNamespace, '_LOGGER': logger_for(events)})[index]
            result = cls().calculate_total_power(None, None, 15)
            outputs.append(vars(result))
        self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(outputs[1]['total_power_kwh'], 0.0)


if __name__ == '__main__':
    unittest.main()
