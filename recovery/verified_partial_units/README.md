# Incomplete and historical compilation contexts

The current partial manifest lists only `weather_fusion_ai/weather_experts.py`:
108 units match, 106 belong to fully verified disjoint owner trees, and
`WeatherExpert._get_json` remains unmatched. This containing module is not a
replacement integration module and must not be imported into a running system.

The other ten files are retained as historical partial contexts. Their current,
completely verified readable sources live under `../verified_units/`; consult
`../verified-manifest.json`. These old copies receive no additional coverage
count and are excluded from the current partial manifest.
