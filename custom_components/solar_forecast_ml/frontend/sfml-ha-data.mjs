// Powered by Solar Forecast ML by Zara-Toorox.
// EiNSTeiN- fork: read only data already exposed to the authenticated HA frontend.
// Distributed under the repository's Proprietary Non-Commercial License.

export function numberOrNull(value) {
  if (value == null || typeof value === 'boolean') return null;
  if (typeof value === 'string' && !value.trim()) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function numberState(hass, id, expectedUnit) {
  const state = hass.states?.[id];
  const value = numberOrNull(state?.state);
  if (value == null) return null;
  const unit = state?.attributes?.unit_of_measurement;
  if (unit === expectedUnit) return value;
  if (expectedUnit === 'W' && unit === 'kW') return value * 1000;
  if (expectedUnit === 'kWh' && unit === 'Wh') return value / 1000;
  return null;
}

function totalIfComplete(values) {
  return values.length && values.every(value => value != null)
    ? values.reduce((total, value) => total + value, 0) : null;
}

function timeContext(hass, now) {
  const timezone = hass.config?.time_zone || 'UTC';
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: timezone, hour: '2-digit', hourCycle: 'h23',
  }).formatToParts(now);
  return { timezone, now: now.toISOString(), current_hour: Number(parts.find(p => p.type === 'hour').value) };
}

export function forecastSnapshot(hass, config = {}, now = new Date()) {
  const prefix = config.entity_prefix || 'sensor.solar_forecast_ml';
  const powerIds = config.panel_entities || Object.keys(hass.states || {})
    .filter(id => id.startsWith(`${prefix}_`) && hass.states[id]?.attributes?.source === 'panel_group_power_sensor')
    .sort();
  const panels = powerIds.map(id => {
    const power = hass.states[id];
    const energyId = id.replace(/_power$/, '_actual_today');
    return {
      name: power?.attributes?.friendly_name || power?.attributes?.group_name || id,
      power: numberState(hass, id, 'W'),
      actual_today_kwh: numberState(hass, energyId, 'kWh'),
    };
  });
  const actual = numberState(hass, config.actual_entity || `${prefix}_sfml_total_actual_today`, 'kWh')
    ?? totalIfComplete(panels.map(panel => panel.actual_today_kwh));
  const livePower = numberState(hass, `${prefix}_sfml_total_power`, 'W')
    ?? totalIfComplete(panels.map(panel => panel.power));
  const hourly = hass.states?.[config.hourly_entity || `${prefix}_rest_of_day`]?.attributes?.hours || {};
  const rows = Object.entries(hourly).flatMap(([hour, value]) => {
    if (!/^\d{2}:00$/.test(hour)) return [];
    const target_hour = Number(hour.slice(0, 2));
    const prediction_kwh = numberOrNull(value);
    return target_hour < 24 && prediction_kwh != null && prediction_kwh >= 0
      ? [{ target_hour, prediction_kwh, actual_kwh: null }] : [];
  });
  const peak = rows.filter(row => row.prediction_kwh > 0)
    .reduce((best, row) => !best || row.prediction_kwh > best.prediction_kwh ? row : best, null);
  const peakHour = peak?.target_hour ?? null;
  return {
    summary: {
      today: {
        production: actual,
        forecast: numberState(hass, config.forecast_entity || `${prefix}_expected_daily_production`, 'kWh'),
        forecast_tomorrow: numberState(hass, `${prefix}_forecast_tomorrow`, 'kWh'),
        accuracy: null, // No accuracy sensor/contract is exposed; do not invent a zero.
        peak_hour: peakHour,
      },
      time_context: timeContext(hass, now),
    },
    energyFlow: {
      statistics: { solar_yield_daily: actual },
      flows: { solar_power: livePower },
      panels,
      time_context: timeContext(hass, now),
    },
    hourly: rows,
  };
}

export function weatherSnapshot(hass, config = {}, now = new Date()) {
  const entity = config.weather_entity || Object.keys(hass.states || {}).find(id => id.startsWith('weather.'));
  const weather = hass.states?.[entity];
  if (!weather || ['unknown', 'unavailable'].includes(weather.state)) return { success: false };
  const attributes = weather.attributes || {};
  let wind = numberOrNull(attributes.wind_speed);
  const windUnit = attributes.wind_speed_unit;
  if (wind != null) {
    if (windUnit === 'm/s') wind *= 3.6;
    else if (windUnit === 'mph') wind *= 1.609344;
    else if (windUnit === 'kn') wind *= 1.852;
    else if (windUnit !== 'km/h') wind = null;
  }
  const temperature = numberOrNull(attributes.temperature);
  const feelsLike = numberOrNull(attributes.apparent_temperature);
  const temperatureUnit = attributes.temperature_unit;
  const toCelsius = value => value == null ? null : temperatureUnit === '°F'
    ? (value - 32) * 5 / 9 : temperatureUnit === '°C' ? value : null;
  return {
    success: true,
    source: entity,
    time_context: timeContext(hass, now),
    current: {
      condition: weather.state,
      temperature: toCelsius(temperature),
      feels_like: toCelsius(feelsLike),
      humidity: numberOrNull(attributes.humidity),
      wind_speed: wind,
      uv_index: numberOrNull(attributes.uv_index),
    },
    // These are not exposed by a native weather entity; keep them absent.
    astronomy: {}, radiation: {}, forecast: [],
  };
}
