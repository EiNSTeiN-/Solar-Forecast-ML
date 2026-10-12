# Verified source recovery work

Powered by Solar Forecast ML by Zara-Toorox. All recovered derivatives remain
subject to the repository [LICENSE](../LICENSE).

This is a development workspace for the full source-recovery goal, not a second
installed integration. `verified_units/` contains only modules whose complete
nested code trees match the recovered bytecode under the documented comparison
rules. `verified-manifest.json` records source/input hashes and distinguishes
bytecode fidelity from readability cleanup and behavioral test coverage.

See [the methodology](../docs/SOURCE_RECOVERY_METHODOLOGY.md) for transformations,
pitfalls, scope and reproducible commands. `progress.json` reports coverage of
all 274 protected payloads, including the failures. Syntax success or a subset
of verified files must not be used to mark the whole goal complete.

Private working directories contain the bytecode and sequence data needed to
repeat static verification. Sequence headers contain cryptographic material;
keep them, logs and vendor runtime binaries outside this repository.

Run the selected pure-function behavior checks with a CPython 3.14 interpreter:

```bash
SFML_RECOVERED_DIR=/private/recovery/recovered \
SFML_UNPACKER_DIR=/tools/unpacker \
python3.14 -B -m unittest discover -s recovery/tests -v
```

The tests check complete reconstructed modules statically before instantiating
selected panel-group, radiation, billing, consumer, weather and planning
functions, coordinator helpers, both Kalman filters, the price calculator,
the Energy AI feature engineer, tariff sources, demo persistence and weather
platform with controlled globals and explicit dependency stand-ins. Further
checks cover tariff windows/thresholds, battery integration, invoice validation,
forecast regeneration/readers, astronomical gating, heat-pump source resolution,
capability gates and isolated dashboard/initialization trees.
Sensor collection/startup, panel calibration and snow, astronomy cache rows and
production windows, and price-statistics storage are also compared, including
handled database failures and cancellation. The current checkpoint publishes
48 readable whole modules and passes 47 selected application test methods.
Known defaults for
pure functions are supplied explicitly. They do not import Home Assistant, an
integration module, or the native PyArmor runtime. Existing merge mutation
behavior is compared explicitly; cleanup must not silently change it.

Isolated source units are published separately under `verified_functions/`.
Their class shells provide compilation context and must not be treated as
complete integration modules. `verified-functions.json` records exact scope,
hashes and the reason for retaining a unit. The audit validates each retained
unit against its original bytecode before inserting its definition into an
incomplete source candidate and comparing the full candidate. Units marked
`retain_in_candidate: false` remain independent evidence and are not inserted
or counted as corpus matches when their caller is missing. With
`--verified-source recovery/verified_units`, the adjacent manifest is detected
automatically; `--verified-functions` can select it explicitly. The generic GPM
correction-revert parser regression is now fixed; its 13 controlled paths still
test the independently verified method body. A second isolated coroutine tests
pipeline success/failure, handled exceptions and cancellation with an explicit
`self` closure. Its enclosing coordinator remains incomplete. Neither isolated
unit currently needs or receives an audit override. Two additional complete
definition trees reconstruct dashboard KPIs and background initialization.
Their verified body overrides preserve existing headers and registration
decorators and restore the three source units still missing from raw
generation, and they receive full static subtree checks before insertion. Eight
dashboard and fourteen initialization scenarios compare error routes, logs,
dependency calls, cache values and queued tasks using only owned stand-ins.
