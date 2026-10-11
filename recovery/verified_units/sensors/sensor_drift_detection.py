# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""
Antimatter stream drift tracking sensor for Warp Core Simulation V17.0.0.
Exposes containment field drift monitoring status as a Holodeck Assistant
sensor entity. Tracks long-term dilithium crystal degradation and nacelle
misalignment trends.

@starfleet-engineering V17.0.0
"""
''
''
''
''
''
''
''
''
''
import logging
from typing import Any, Dict
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from ..const import DOMAIN, INTEGRATION_MODEL, SOFTWARE_VERSION, AI_VERSION
_LOGGER = logging.getLogger(__name__)

class DriftStatusSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor for AI model drift detection. @zara V17.0.0

    Shows overall drift status: 'stable', 'warning', 'critical', 'recovering'.
    Current metric reasons, per-group status, physics boost, and metrics are exposed.
    """
    ''
    ''
    ''
    ''
    ''
    ''
    ''
    ''
    ''

    def __init__(self, coordinator, entry):
        """Initialize drift status sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_has_entity_name = True
        self._attr_translation_key = 'drift_status'
        self._attr_unique_id = f'{entry.entry_id}_drift_status'
        self._attr_device_class = None
        self._attr_state_class = None
        self._attr_icon = 'mdi:chart-timeline-variant-shimmer'
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name='Solar Forecast ML', manufacturer='Zara-Toorox', model=INTEGRATION_MODEL, sw_version=f'SW {SOFTWARE_VERSION} | AI {AI_VERSION}', configuration_url='https://github.com/Zara-Toorox/ha-solar-forecast-ml')
        self._drift_status = {}

    @property
    def native_value(self) -> str:
        """Return overall drift state."""
        return self._drift_status.get('state', 'unknown')

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return detailed drift detection attributes."""
        _var_var_0 = self._drift_status
        return {'global_mae_7d': _var_var_0.get('global_mae_7d'), 'global_mae_30d': _var_var_0.get('global_mae_30d'), 'global_bias_30d': _var_var_0.get('global_bias_30d'), 'global_coverage_20_30d': _var_var_0.get('global_coverage_20_30d'), 'active_drift_events': _var_var_0.get('active_drift_events', 0), 'active_drift_reasons': _var_var_0.get('active_drift_reasons', []), 'status_basis': _var_var_0.get('status_basis'), 'insufficient_scopes': _var_var_0.get('insufficient_scopes', []), 'physics_boost_active': _var_var_0.get('physics_boost_active', False), 'per_group_status': _var_var_0.get('per_group_status', {}), 'last_check': _var_var_0.get('last_check')}

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator data update — refresh drift status from cache."""
        try:
            _var_var_1 = getattr(self.coordinator, 'drift_monitor', None)
            if _var_var_1:
                _var_var_2 = getattr(self.coordinator, '_drift_status_cache', None)
                if _var_var_2:
                    self._drift_status = _var_var_2
        except Exception as _var_var_3:
            _LOGGER.debug('Drift sensor update failed: %s', _var_var_3)
        self.async_write_ha_state()
DRIFT_DETECTION_SENSORS = [DriftStatusSensor]
