import assert from 'node:assert/strict';
import test from 'node:test';
import { numberOrNull, forecastSnapshot, weatherSnapshot } from '../custom_components/solar_forecast_ml/frontend/sfml-ha-data.mjs';

const state = (value, unit, attributes = {}) => ({ state: value, attributes: { unit_of_measurement: unit, ...attributes } });
const fixture = () => ({ config: { time_zone: 'America/Toronto' }, states: {
  'sensor.solar_forecast_ml_gruppe_1_power': state('200', 'W', { source: 'panel_group_power_sensor', friendly_name: 'East' }),
  'sensor.solar_forecast_ml_gruppe_1_actual_today': state('1000', 'Wh'),
  'sensor.solar_forecast_ml_gruppe_2_power': state('0.1', 'kW', { source: 'panel_group_power_sensor', friendly_name: 'West' }),
  'sensor.solar_forecast_ml_gruppe_2_actual_today': state('2', 'kWh'),
  'sensor.solar_forecast_ml_rest_of_day': state('2', 'kWh', { hours: { '13:00': 0, '14:00': 1.25, '15:00': 0.75 } }),
  'sensor.solar_forecast_ml_expected_daily_production': state('unavailable', 'kWh'),
  'sensor.solar_forecast_ml_forecast_tomorrow': state('10', 'kWh'),
} });

test('unknown, unavailable, empty, nonfinite and boolean values stay missing; zero is valid', () => {
  for (const value of [null, undefined, '', ' ', 'unknown', 'unavailable', true, NaN, Infinity]) assert.equal(numberOrNull(value), null);
  assert.equal(numberOrNull('0'), 0);
});

test('panel powers and energy normalize units and sum without double-counting total sensors', () => {
  const snapshot = forecastSnapshot(fixture());
  assert.equal(snapshot.energyFlow.flows.solar_power, 300);
  assert.equal(snapshot.summary.today.production, 3);
  assert.equal(snapshot.summary.today.forecast, null);
  assert.equal(snapshot.summary.today.accuracy, null);
  assert.equal(snapshot.summary.today.forecast_tomorrow, 10);
  assert.equal(snapshot.energyFlow.panels.length, 2);
  assert.equal(snapshot.hourly[0].prediction_kwh, 0);
  assert.ok(snapshot.hourly.every(row => row.actual_kwh === null));
});

test('one offline array prevents an incomplete sum from appearing as a complete system total', () => {
  const hass = fixture();
  hass.states['sensor.solar_forecast_ml_gruppe_2_power'].state = 'unavailable';
  hass.states['sensor.solar_forecast_ml_gruppe_2_actual_today'].state = 'unknown';
  const snapshot = forecastSnapshot(hass);
  assert.equal(snapshot.energyFlow.flows.solar_power, null);
  assert.equal(snapshot.summary.today.production, null);
});

test('explicit total sensors take precedence over panel fallback sums', () => {
  const hass = fixture();
  hass.states['sensor.solar_forecast_ml_sfml_total_power'] = state('350', 'W');
  hass.states['sensor.solar_forecast_ml_sfml_total_actual_today'] = state('4', 'kWh');
  const snapshot = forecastSnapshot(hass);
  assert.equal(snapshot.energyFlow.flows.solar_power, 350);
  assert.equal(snapshot.summary.today.production, 4);
});

test('malformed and unavailable hourly forecasts are omitted, not plotted as zeros', () => {
  const hass = fixture();
  hass.states['sensor.solar_forecast_ml_rest_of_day'].attributes.hours = { '24:00': 1, '1:00': 1, '12:00': 'unavailable', '13:00': -1, '14:00': 0 };
  assert.deepEqual(forecastSnapshot(hass).hourly, [{ target_hour: 14, prediction_kwh: 0, actual_kwh: null }]);
});

test('clock uses HA timezone, not browser timezone', () => {
  assert.equal(forecastSnapshot(fixture(), {}, new Date('2026-10-11T18:00:00Z')).summary.time_context.current_hour, 14);
});

test('weather uses selected entity and converts known native units', () => {
  const hass = fixture();
  hass.states['weather.example'] = { state: 'cloudy', attributes: { temperature: 68, temperature_unit: '°F', wind_speed: 5, wind_speed_unit: 'm/s', humidity: 0 } };
  const snapshot = weatherSnapshot(hass, { weather_entity: 'weather.example' });
  assert.equal(snapshot.success, true);
  assert.equal(snapshot.current.temperature, 20);
  assert.equal(snapshot.current.wind_speed, 18);
  assert.equal(snapshot.current.humidity, 0);
  assert.deepEqual(snapshot.radiation, {});
  assert.deepEqual(snapshot.forecast, []);
});

test('offline weather and missing readings are not fabricated', () => {
  assert.deepEqual(weatherSnapshot(fixture()), { success: false });
  const hass = fixture();
  hass.states['weather.example'] = { state: 'sunny', attributes: { temperature: 10, temperature_unit: 'bad-unit', wind_speed: 1, wind_speed_unit: 'bad-unit' } };
  const snapshot = weatherSnapshot(hass);
  assert.equal(snapshot.current.temperature, null);
  assert.equal(snapshot.current.wind_speed, null);
});
