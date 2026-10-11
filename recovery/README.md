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
the selected panel-group functions, coordinator helpers, and both Kalman filter
classes with controlled globals. They do not import Home Assistant, an
integration module, or the native PyArmor runtime. Existing merge mutation
behavior is compared explicitly; cleanup must not silently change it.
