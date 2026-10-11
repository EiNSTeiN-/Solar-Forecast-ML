# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Private product and entitlement policy for offline EAI licenses."""
''
''
''
''
''
''
''
''
''
PRODUCT = 'solar_forecast_eai'
PREFIX = 'EAI1'
SIGNING_CONTEXT = b'solar_forecast_eai-license-v1\x00'
REQUIRED_ENTITLEMENT = 'eai_enabled'
KNOWN_ENTITLEMENTS = frozenset({REQUIRED_ENTITLEMENT, 'forecast_enabled', 'building_analysis_enabled', 'advanced_diagnostics_enabled'})
MAX_KEY_LENGTH = 4096
MAX_PAYLOAD_BYTES = 2048
MAX_ENTITLEMENTS = 16
MAX_STRING_LENGTH = 256
