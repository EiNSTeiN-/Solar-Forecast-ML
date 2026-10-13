# Remaining coroutine-return coverage difference

Seventeen reconstructed coroutine units match all normalized instructions and
signatures, but fail the unchanged exception-table proof. Every earlier matching
neighbor is retained. These candidates remain private, unaccepted, and excluded
from corpus and readable-source counts. The original exports are unchanged.

Each difference affects one **canonical** `RETURN_VALUE`: the reference has no
handler at that comparison position, while stock CPython 3.14.2 assigns a
depth-zero, `lasti`-preserving route to `INTRINSIC_STOPITERATION_ERROR`. Raw
opcode mapping now identifies all seventeen positions as protected forward
jumps that the existing shared-return rule represents as returns. They are not
raw return instructions. The jumps originally have non-lasti user-handler
coverage, which the existing alias rule removes from the canonical dispatch
map. Their effective normalized exit targets retain intrinsic cleanup coverage.
No application handler differs in the nearest private candidates after that
normalization. These observations do not establish whole runtime equivalence
or prove general source impossibility.

CPython establishes an outer cleanup wrapper for coroutines. Its unwind logic
keeps that wrapper active while emitting a return, and flowgraph construction
assigns handlers before optimization. The directed source trials preserve
coroutine flags and the business expressions; they cannot remove this final
cleanup interval under that compiler model. Sources:
[wrapper construction](https://github.com/python/cpython/blob/v3.14.2/Python/codegen.c#L1176-L1190),
[unwind logic](https://github.com/python/cpython/blob/v3.14.2/Python/codegen.c#L518-L527),
[return emission](https://github.com/python/cpython/blob/v3.14.2/Python/codegen.c#L2191-L2226),
[handler assignment](https://github.com/python/cpython/blob/v3.14.2/Python/flowgraph.c#L885-L968).

The owned regression [test_intrinsic_return_coverage.py](../recovery/tests/test_intrinsic_return_coverage.py)
compiles a literal coroutine and removes only that return interval from its own
code object. Instructions remain byte-identical and five ordinary calls agree,
including errors. Strict comparison correctly rejects the exception difference.
A return tracing hook that raises `StopIteration` distinguishes the programs:
stock code converts it into `RuntimeError('coroutine raised StopIteration')`;
the owned return-hole control propagates `StopIteration`. Both tests pass.
No production bytecode is modified or executed in this test.

The owned real-return-hole difference is **observable**, even though ordinary
owned calls agree. It remains a useful negative control against arbitrary
exception removal. It does not reproduce the protected jump/return provenance
of these seventeen production comparison positions and must not be presented
as a direct runtime counterexample for them. Actual user-return sites in the
raw protected code retain intrinsic routes; runtime-exit normalization makes
those earlier physical returns unreachable after replacing an exit jump.
The final protected leave/return tail is separate from the disputed alias.

Any proposed alias-specific treatment still needs a precise rule, controls
that retain every real return and meaningful handler, and an explicit decision
before expanding the current accepted comparison policy. No blanket runtime
equivalence claim follows, and no arbitrary exception region may be ignored.
Until then, the strict result remains failure. The separately unresolved
WeatherExpert instruction-layout mismatch is not covered by this classification.

## Classified units

Indices below refer to normalized instruction positions; private bytecode,
sequence headers, and cryptographic material are omitted. The machine-readable
evidence is [coroutine-return-coverage.json](../recovery/coroutine-return-coverage.json).

| Module | Unit | Matching instructions | Return index |
| --- | --- | ---: | ---: |
| `data/data_hourly_predictions.py` | `<module>/HourlyPredictionsHandler#1/get_predictions_count#1` | 64 | 30 |
| `data/data_ml_handler.py` | `<module>/DataMLHandler#1/get_hourly_samples_count#1` | 64 | 30 |
| `data/data_operational_forecast_handler.py` | `<module>/DataOperationalForecastHandler#1/create_snapshot#1` | 98 | 60 |
| `data/data_operational_forecast_handler.py` | `<module>/DataOperationalForecastHandler#1/get_active_daily_forecast#1` | 69 | 33 |
| `extra_features/grid_price_monitor/storage/price_cache.py` | `<module>/PriceCache#1/async_get_country#1` | 39 | 20 |
| `extra_features/solar_forecast_eai/consumption/consumption_scheduled_tasks.py` | `<module>/ScheduledTasksManager#1/_get_forecast_radiation_for_hour#1` | 48 | 29 |
| `extra_features/solar_forecast_eai/data/data_hourly_predictions.py` | `<module>/HourlyPredictionsHandler#1/get_predictions_count#1` | 64 | 30 |
| `extra_features/solar_forecast_eai/data/data_ml_handler.py` | `<module>/DataMLHandler#1/get_hourly_samples_count#1` | 64 | 30 |
| `extra_features/solar_forecast_eai/data/data_ml_handler.py` | `<module>/DataMLHandler#1/get_max_dni_for_hour#1` | 67 | 32 |
| `extra_features/solar_forecast_eai/data/data_ml_handler.py` | `<module>/DataMLHandler#1/get_seasonal_factor#1` | 67 | 32 |
| `extra_features/solar_forecast_eai/data/data_weather_corrector.py` | `<module>/WeatherForecastCorrector#1/_get_sun_elevation#1` | 67 | 48 |
| `extra_features/solar_forecast_eai/data/data_weather_corrector.py` | `<module>/WeatherForecastCorrector#1/_get_weighted_poa_radiation#1` | 67 | 48 |
| `extra_features/solar_forecast_eai/data/data_weather_expert_blender.py` | `<module>/BrightSkyExpert#1/get_cloud_cover#1` | 55 | 23 |
| `extra_features/solar_forecast_eai/data/data_weather_expert_blender.py` | `<module>/OpenMeteoExpert#1/get_cloud_cover#1` | 42 | 23 |
| `extra_features/solar_forecast_eai/data/data_weather_expert_blender.py` | `<module>/PirateWeatherExpert#1/get_cloud_cover#1` | 61 | 29 |
| `extra_features/solar_forecast_eai/data/data_weather_expert_blender.py` | `<module>/WttrInExpert#1/get_cloud_cover#1` | 55 | 23 |
| `production/production_scheduled_tasks.py` | `<module>/ScheduledTasksManager#1/_resolve_eod_actual_yield#1` | 111 | 75 |
