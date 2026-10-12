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
58 readable whole modules and passes 77 selected application test methods.
Physics, neural-network gradients/training/restore and controlled repair
transactions are now covered too. The MLP checks require NumPy; this checkpoint
was tested with `numpy==2.5.4` in the CPython 3.14 recovery environment.
The latest checks also cover policy noise bands and champion age, weather-energy
date/dev gates and callback/query paths, visibility provider priority and both
fog/clear contradiction directions. Controlled relative imports use explicit
owned dependency stand-ins.
Seven additional generator code units have readable compilation contexts in
`verified_generators.py` and `verified_annotated_generators.py`. Their wrappers
are not replacement integration APIs or verified containing methods.
`verified-generators.json` identifies the exact original/candidate unit paths
and source hashes. Five test methods make 37 paired comparisons of filters,
getter/truth order, coverage gates, captured finite checks, laziness, errors,
and the original generator StopIteration conversion. The original future flag
is retained explicitly in the contexts that require it. These units already
match in raw generation and receive no overrides or second coverage count.
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

Two additional readable isolated coordinator lookups preserve independent
rejection guards and the assignment that their returns rely on. They receive
full unit proof and four test methods covering 23 input/error scenarios,
including false-valued coordinators, missing HASS, repeated context reads and
truth conversion that must never run. The EMS class is compilation context
only; the larger EMS and stats API modules remain incomplete. The generic
parser recovers both functions directly, so they receive no body override or
second corpus count.

The current shared-return checkpoint adds readable complete shadow-forecast,
shadow-observation and GPM-entitlement modules, plus two independently verified
diagnostic/range functions. Internal locals have descriptive names. Thirty-five
unnecessary folded-expression layers were removed with full-module proof after
every change; the few retained edges preserve the compiler's shared blocks.
Five tests compare 49 formatter, entitlement-constructor and range-guard
scenarios. Four more cover shadow thresholds, seasonal blending, physics caps,
geometry, weather/sensor gates, observation classes and sensorless residuals.
The entitlement constructor is tested from its complete verified module rather
than kept as a redundant isolated source. Neither isolated function receives an
override or extra corpus count. The whole goal remains incomplete at
13,283 of 14,291 matched units, with 60 complete module matches.

The protected-return checkpoint restores existing return evaluations to their
original handlers, including nested row-access fallbacks. It adds 22 matches
from repaired source and one from a documented extension to the existing
forward-jump-to-bare-return comparison. The latter ignores only that jump's
own non-lasti dispatch slot; coverage over calls, cleanup, backward jumps and
real returns remains significant. Old sources are audited separately under
the new rule so the counts cannot conflate those gains.
Five additional readable functions have 82 paired conversion, timezone and
row-access scenarios. The same readable row accessor is independently proved
against both original statistics modules. There are now 12 isolated source
files proving 18 distinct units, with no new overrides or duplicate coverage.
The two newly complete generated modules, energy efficiency and daily
briefing, still need whole-module readability cleanup and publication.
