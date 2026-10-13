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
186 readable whole modules (11,108 nested units) and passes 206 selected
test methods (196 application checks and ten source/coverage checks). The full
corpus has 263 completely matched candidate modules and 14,273/14,291 matching
units; 18 units remain. Candidate fidelity
and published readable source are counted separately.
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

The shared-return checkpoint added readable complete shadow-forecast,
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
13,449 of 14,291 matched units, with 72 complete module matches.

The protected-return checkpoint restores existing return evaluations to their
original handlers, including nested row-access fallbacks. It adds 22 matches
from repaired source and one from a documented extension to the existing
forward-jump-to-bare-return comparison. The latter ignores only that jump's
own non-lasti dispatch slot; coverage over calls, cleanup, backward jumps and
real returns remains significant. Old sources are audited separately under
the new rule so the counts cannot conflate those gains.
Five additional readable functions have 82 paired conversion, timezone and
row-access scenarios. The same readable row accessor is independently proved
against both original statistics modules, with no new overrides or duplicate
coverage. The subsequent readability pass publishes the complete energy
efficiency and daily briefing modules, adding 82 readable whole-module units.
Twelve empty-string/pass artifacts and three unnecessary folded layers are
removed with whole-tree proof. Exact thresholds, SQL values, original
documentation and localized text remain intact.
Eleven more test methods cover real stdlib dataclass decoration, sample and
window accounting, summary quality, readiness/clock and database paths,
owned imports, localized rendering, service checks, payloads and cancellation.
The standalone `_number` compilation context is retired; its existing tests
now use the complete efficiency source. That pass retained 13,283 matches
while adding readable evidence and behavioral coverage.

The chained-inline-filter checkpoint fixes an accepting jump that lands at
another filter rather than directly at the append. Its fall-through contains
only padding and one back edge to the active loop. Recognizing that exact
shape preserves each guard's original polarity. Full raw and composite
audits add 45 matches with zero previous matches lost, without changing
comparison rules. Two additional readable isolated accessors cover CSV
headers and EMS hour selection, with four tests over 50 paired scenarios.
Their containing modules remain incomplete. Both match directly in generic
generation; neither receives an override or duplicate corpus count.
At that checkpoint, isolated coverage was 13 source files proving 19 distinct units,
plus the seven generator units. That checkpoint reaches 13,328 matches.

The shared-raise checkpoint preserves each validation predicate, exception
expression and explicit cause, plus the exact comparison-cleanup trampoline
and formatted-message conversions/specifications. It adds 15 matches in both
raw and composite audits with zero previous matches lost and unchanged
comparison rules. Three newly complete modules are published readably:
both license codecs and the heating API, totaling 91 whole-module units.
Their validation, public signatures, import contracts, routes, authorization,
privacy and delegation behavior remain intact. Nine new application methods
test owned encoding payloads/policies and HTTP-view stand-ins, including errors
and cancellation. No real license, HTTP server or actuator is used.
That checkpoint reached 13,343 matches, with 874 instruction and 74
exception-table failures. The full 274-module goal remains incomplete.

The padded-value-return checkpoint preserves the one existing expression at a
recognized return tail instead of replacing it with a bare return. Its
bounded repair adds 15 raw and composite matches with zero prior matches lost;
normalization and comparison rules stay unchanged. It completes the datetime
utility module, whose 28-unit tree is published readably after removing 19
empty artifacts and renaming internal bindings. Seven new methods compare
stdlib timezone/DST and parsing behavior, controlled HA delegation, fallback
errors/logs, day boundaries and the seasonal relative import.
Two additional isolated source files prove both tariff timezone fallbacks and
the recursive JSON conversion helper. Six methods compare 85 owned scenarios,
including constructor/getter errors, false local values, nested conversions,
array/item hooks, mapping iteration and rejected bytes. These three units
match directly in generation, receive no overrides and are counted once.
That checkpoint’s readable evidence covers 64 complete modules / 1,443 units and 15
isolated files / 22 distinct units, plus the seven generator units.
The corpus reaches 13,358 matches, leaving 859 instruction and 74
exception-table failures (933 units). The full source/readability goal remains
active and incomplete; these sources are not deployed to Home Assistant.

The shared-handler-fragment checkpoint reunites duplicated consecutive tries
only after complete code-unit proof, retaining every previously matched unit.
It adds 70 matches across 46 modules with no regression or comparison change.
The raw and composite counts reach 13,422 and 13,428 respectively. Four newly
complete modules are published readably: the astronomy cache manager, peak
tracker, published DEV-state/delegation helper and operational reforecast
engine. Their 73-unit trees match after cleanup and formatting. Twelve new
application methods compare owned database rows/errors/cancellation, cache
reference/singleton behavior, DEV expiry/privacy/callback paths and future-only
reforecast filtering, scaling, publication checks, listener errors and locks.
At that checkpoint, all 126 selected application methods passed. Whole-source coverage was
68 modules / 1,516 readable units, alongside the unchanged 15 isolated files /
22 distinct units and seven generator units. Remaining 789 instruction and
74 exception-table failures leave 863 unverified units. The full recovery goal
remains active and incomplete.

The joint callable/return checkpoint restores three database getter methods in
the statistics forecast, hourly billing and power-source collectors. Their
attribute lookup and shared coroutine exit must be repaired together: either
isolated trial remains a verifier failure. The bounded source trial reuses
only existing calls and return values, requires complete unit proof and retains
every prior match. Comparison and exception-dispatch rules are unchanged.
Raw and composite recovery add three matches with zero losses, reaching
13,425 and 13,431 units respectively. There are still 68 complete composite
modules and 860 unverified units: 786 instruction and 74 exception-table failures.

The three getters have readable isolated class contexts with descriptive local
names, original relative import levels, signatures and branch behavior. They
receive no retained-body override or additional corpus count. Three application
methods compare 90 paired creation, reuse, reconnection, import/property/lookup
error and cancellation scenarios using owned dependencies. All 129 selected
application methods passed at that checkpoint. Coverage was 68 readable whole modules / 1,516
units, 18 isolated source files / 25 distinct units and seven separately counted
generator units. The isolated manifest has 20 entries; its overlapping pipeline
entry counts once in the distinct total. The full source/readability objective
remains active and incomplete.

The decorated-scope checkpoint fixes source lookup for properties and decorated
methods. CPython's code object begins at the first decorator, while two repair
passes had looked up the function's def line. Correcting that mapping lets the
existing callable and shared-return trials run; comparison rules stay unchanged.
Raw and composite recovery add 18 matches with zero regressions, reaching
13,443 and 13,449 units respectively. Four modules become fully matched and
readable: coordinator configuration helpers, the weather database contract,
Weather Fusion sensors and the main diagnostic sensors.

Their 263-unit trees retain full matches after descriptive local naming,
formatting, removal of 261 empty artifacts and eight unnecessary folded
expressions. Distinct comprehension and branch-local bindings keep distinct
names and slots. Thirteen new application methods cover configuration defaults
and precedence, weather rows/queries/weighted summaries/time zones, sensor
calibration, workflow privacy, AI candidate deadlines and scheduler boundaries.
All 142 selected application methods pass. Current whole-source coverage is
72 modules / 1,779 readable units; isolated fixture coverage remains 18 files /
25 distinct units plus seven separately tallied generators. One isolated
diagnostic formatter is now also present in its recovered whole module and
receives no additional corpus credit. The remaining 768 instruction and
74 exception-table failures leave 842 unverified units. The full objective
remains active and incomplete.

The 842-unit investigation and its assignments are documented in
[the investigation report](../docs/INVESTIGATION_842_UNITS.md) and
`investigation-842-units.json`. The corrected checkpoint gains 97 units with
zero losses, reaching 13,546/14,291 and leaving 745. The first backend trial
failed retention despite net gains and passing tests; its mixed-target iterator
regression was repaired before recovered-source publication. Eight new readable
modules add forecast quality, grid-charge gates, SMC context handling, forecast
history, error handling, corrected statistics, and both offline license
validators. Their validation/signature/error decisions remain intact.


The EAI DNI tracker is now readable source with the original rolling seven-day
state, database queries, self-healing bounds and error ownership intact. Four
paired methods cover dates, rollover, snapshot keys, failed saves and
cancellation. All 14 module units match before either class is executed with
owned clock/database stand-ins. Its addition increases the readable subset;
the already matched corpus count is unchanged.


The main and EAI data validators now have readable local names, with both
complete 24-unit modules still proved independently. Five paired methods
cover dictionary bounds and warnings, data-quality thresholds, weather/config
and daily-summary rows, LSTM/attention field checks, report ordering and
database failure/cancellation. The original optional-data behavior and the
different main/EAI clocks are preserved. This adds readable source for already
matched modules; corpus coverage is not counted again.


The weather reader is now readable, retaining its complete 22-unit proof.
Four paired methods cover query filters, current/forecast/archive rows,
optional schema aliases, zero actual values, malformed rows, unavailable
storage, query/construction/exit faults, cancellation, context suppression,
manager reconnect and direct fallback. Only known renamed local names inside
UnboundLocalError diagnostics are normalized in behavioral logs. This is
readable source for an already matched candidate; corpus coverage is unchanged.


The `owner-recipes-01` checkpoint adds 282 fully proved owner definitions in
121 compilation contexts under `verified_functions/owner_recipes/`. Each owner
and all its descendants match original instructions and exception tables.
Their 464 distinct tree units include already matched descendants; the actual
corpus gain is 283 units, with no previous matches or complete modules lost.
The contexts retain source metadata and are deliberately not classified as
readable modules. Only manifest-listed trees are proved; other surrounding
definitions provide compilation context and must not be imported. Including
older isolated evidence, there are 302 entries in 139 source files, covering
489 distinct units, of which 284 owner definitions are retained in candidates.
Raw generic coverage remains 13,910 units and 134 modules on native 0.6.25
and unpacker 0.4.31. These are source reconstructions, not generic tool gains.
The full selected suite passes 192 methods. Two new application methods make
42 paired candidate-reconciliation comparisons covering validation, selection,
disposition writes, transaction lifecycle, query failures, cancellation and
suppression. Three source-path methods check compound-suite ownership and
definition occurrence lookup. No integration module or armor runtime executes.


The append-only `owner-recipes-02` checkpoint adds sixteen owner definitions
(26 complete owner/descendant units), gaining sixteen corpus units with zero
prior losses. Shadow query lifetime, datetime parsing, schema-loop guards,
hourly forecast fallbacks, learning diagnostics and CSV row/handler ownership
are restored. Every older owner in the fifteen changed contexts is re-proved
before updating its hash. There are now 318 isolated entries in142 source
files, covering515 distinct units;300 definitions are retained in candidates.
The192 selected methods and their executed source inputs remain unchanged.
Full static source/subtree/hash checks cover every new and existing entry.
The raw decompiler and readable-whole-module totals remain unchanged.


The CSV tariff importer is now readable, with its complete50-unit proof.
Four application methods make130 paired comparisons covering CSV profiles,
header detection, numeric formats, units, BOM/encoding, timezones, grouped
hour prices, community weighting, malformed/old/future/empty rows, mappings,
resource limits, restoration, correction writes, original price/source
retention, daily statistics, revision increments, failures and cancellation.
Only controlled dependencies and owned data/database fixtures execute after
the full module proof. The selected suite now passes196 methods. This adds
readability for an already matched module, with no additional corpus gain.


The `owner-recipes-03` checkpoint adds 27 fully proved owner definitions across
17 changed compilation contexts. Their 99 distinct owner-tree units include
already matched descendants; the corpus gains 28 units with zero losses.
The complete database manager now matches all 717 units, including deadline,
savepoint, provenance and candidate persistence paths. Other reconstructions
restore daily hygiene, forecasting, summary queries, weather correlations,
consumer settings, hourly updates and export lifetimes. All old owner trees in
changed contexts are re-proved. There are now 345 isolated entries in 149
source files, covering 612 distinct units; 327 definitions are retained in
candidates. These contexts are not classified as readable whole modules.
The selected 196-method suite's executed bodies remain unchanged; complete
source/hash/subtree checks validate every current published entry. Stable
native .25 and source API .31 remain unchanged. A .29 native trial gains 15 raw
units but loses one existing reader; its evidence is retained and it is held
out of stable main. Full source equivalence and readability remain incomplete.


The `owner-recipes-04` checkpoint adds 12 exact owner bodies across 8
changed contexts, gaining 14 corpus matches with zero losses. Its 25
proved owner-tree units include previously matched descendants. Reconstructions
restore live HTTP response ownership, omitted hourly weather data, optional snow
lookups, method-learning writes, conservative planning row guards, morning retry
alerts, training notifications and end-of-day nested status captures. Every old
and new public source/hash/subtree proof passes. There are 357
isolated entries in 150 files, covering
637 distinct units; 339
definitions are retained. These new contexts remain non-readable. Stable .25/.31
tools and the selected suite's executed function bodies remain unchanged.


The database manager is now readable whole source with all 717 nested units
proved. The main thread names 1,457 local/capture bindings across 316 scopes,
preserving physical closure order, signatures, defaults, annotations, dependency
symbols and docstrings. Twenty-seven standalone empty artifacts are removed;
compiler-sensitive guards and returns remain where their complete proofs require
them. Three new methods compare 60 owned transaction scenarios: write-lock and
owner/depth handling, successful and failed BEGIN/COMMIT, active partial BEGIN,
savepoint release/rollback, body failures/cancellation, closed/unmanaged storage,
future/expired/active deadlines and progress-handler restoration. Complete proof
precedes each selected function's controlled execution. The full selected suite
passes 199 methods. Readable coverage becomes 101 modules/3,354 units; the already
proved database receives no additional corpus match credit.


The `owner-recipes-05` checkpoint adds 5 exact owner bodies across 4
changed contexts, gaining 5 corpus matches with zero losses. Its 5
proved owner-tree units include previously matched descendants. Coupled accepting guards and original result scopes restore shadow data, correction factors and solar totals. Guard-only and return-only repairs remain unproved; complete owners decide the combined reconstruction. Every old
and new public source/hash/subtree proof passes. There are 362
isolated entries in 152 files, covering
642 distinct units; 344
definitions are retained. These new contexts remain non-readable. The frozen
.25/.31 generation tools and selected suite's executed bodies remain unchanged.


## Promoted async-exit recovery tools

Native **0.6.30** and unpacker **0.4.34** are promoted to both forks' main
branches after complete frozen retention and current-source re-audits. Fifteen
new raw matches reach **13,925/14,291 /137 complete raw candidates**, with zero
prior losses. Existing owner reconstructions already cover those units, so
composite coverage stays **14,261/14,291 /251 complete candidates**, with
**30 remaining**. All 274 sources compile. Readable source remains
101 modules/3,354 units; the selected suite passes199 methods.

The source API and comparator remain byte-identical to .31. A calculated return
or nested exit protected by a known context handler keeps its original protocol,
including displaced fragments that have lost lexical blocks. Unprotected/open
context walks retain their normal exit handling. The original reader again
proves all68units. Earlier .27/.29 failed stages remain preserved and excluded
from success counts. Source reconstruction, native gains and readability are
separately tallied.

Main-branch CI passes211 unpacker cases with one existing expected failure,
73 native behavior methods, native builds/tests on Linux/macOS/Windows, clippy
correctness, C++ builds and Actions security checks. Local Rust tests pass35.
No test or corpus gate broadens the comparison rules or imports the integration.
All three goals continue; complete source/readable recovery is unfinished.


The `owner-recipes-06` checkpoint adds 7 exact owner bodies across 7
changed contexts, gaining 7 corpus matches with zero losses. Its 15
proved owner-tree units include previously matched descendants. Reconstructed owners restore original work and its guards, contexts and error scopes. Every old
and new public source/hash/subtree proof passes. There are 369
isolated entries in 154 files, covering
657 distinct units; 351
definitions are retained. These new contexts remain non-readable. The frozen
0.6.30-einstein.1/0.4.34-einstein.1 generation tools and selected suite's executed bodies remain unchanged.


## Scope-aware readable sensor, solar API and adaptive source

Three root-authored files now provide readable whole source for **614 already
matched units**: the main sensor layer443, STATS solar API140, and adaptive
forecast engine31. The final source names **1,089 local/capture bindings** in
149 actual code-object scopes and removes315 non-docstring empty artifacts.
Names follow data flow, existing fields and helper roles; original spelling is
not claimed. Actual parameters, globals/attributes, imports, schemas, signatures,
decorators/defaults/annotations, flags and docstrings remain fixed.

Generator targets belong to their actual `<genexpr>` code scopes, even when an
investigatory proposal groups them under its enclosing function. Split those
bindings before renaming; preserve `.0`, lambda parameters and lexical shadows.
Free references follow their owning binding. Nine solar API aliases and fourteen
sensor datetime aliases get descriptive local names while the real imported
modules, relative levels, symbols and ordering stay unchanged. Physical closure
ordering remains part of the complete proof. Removing the solar API's single
dead private compiler slot loses its instruction proof, so that required slot
remains with an explanation; it performs no runtime work.

All three complete trees still prove exact normalized instructions **and
exception tables** after Black formatting. They increase readable coverage to
**104 modules / 3,968 units**, with **zero new corpus match credit**. Every old and
new public source/hash/subtree proof passes. The selected suite passes
**201 methods in168.722seconds** (196 application methods plus five source
construction methods); no complete integration or armor runtime is imported.

The candidate auditor also preserves verified multiline return layout after
AST body insertion. Two owned assembly regressions compare19 paired calls,
check keyword defaults/docstrings, and reject a wrong retained body. Successful
layout recovery does not certify that body's semantics: independent complete
candidate comparison remains mandatory. The generic .35 corpus gate is recorded
separately from these already-matched readability changes.


The `owner-recipes-07` checkpoint adds 1 exact owner bodies across 1
changed contexts, gaining 1 corpus matches with zero losses. Its 4
proved owner-tree units include previously matched descendants. Reconstructed owners restore original work and its guards, contexts and error scopes. Every old
and new public source/hash/subtree proof passes. There are 370
isolated entries in 154 files, covering
661 distinct units; 352
definitions are retained. These new contexts remain non-readable. The frozen
0.6.30-einstein.1/0.4.34-einstein.1 generation tools and selected suite's executed bodies remain unchanged.


## Readable coordinator and actual-state source

Two additional complete modules expose 169 already-proved units with 239
descriptive local/capture bindings across 49 scopes. They remove 72 non-docstring
empty artifacts. Complete instruction and exception proofs still pass after
Black formatting. All current public source hashes and complete/subtree proofs
pass: **106 readable modules / 4,137 units**. This changes no corpus match count.
Public parameters, global/attribute names, imported objects, literal schemas,
signatures, flags, documentation, and ordered closure slots remain fixed.

Two separate owned tests document the remaining coroutine-return cleanup
boundary; they supplement the previously passed 201-method selected suite.
They intentionally show both strict rejection and an observable tracing-hook
difference. See [the coverage report](../docs/COROUTINE_RETURN_COVERAGE.md).
No production reference bytecode or comparator rule is changed.


## Promoted multiline return layout

Unpacker **0.4.35** is promoted to the fork's main branch after fresh complete
source generation for all274 payloads and strict full-corpus retention.
Its bounded layout pass restores the existing shared return block in the STATS
hourly price method. Raw coverage reaches **13,926/14,291 /138 complete modules**.
Re-auditing the frozen current106-module source overlay reaches
**14,270/14,291 /260 complete candidate modules**, with **21 remaining**
(four instruction failures and seventeen exception-coverage failures).
Both gates retain every previous matching unit and complete module.

No input export or exception comparison rule changed. Native0.6.30 remains pinned
to its previously tested binary/code hashes. Main-branch CI passes216 unpacker
cases (215passes and one existing expected failure), C++ builds and Actions
security checks. The workflow named Regression Test is skipped and is not
counted as passed. The three newly proved .36 return joins are a separate
development checkpoint until their fresh274-module gate finishes.


## Readable heating, insights, weather precision and forecast collection

Six root-authored naming refactors publish **845 already-proved units** with
**1,827 descriptive bindings across346 actual code scopes**. Ninety non-docstring
empty artifacts are removed. Complete instructions and exception tables remain
exact after Black formatting, and every current public source/hash/subtree proof
passes. Readable coverage reaches **112 modules / 4,982 units**; this adds **zero
corpus matches** and changes no comparison rule.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `sensors/sensor_shadow_detection.py` | 63 | 96 | 20 |
| `extra_features/solar_forecast_eai/data/data_weather_precision.py` | 41 | 185 | 20 |
| `extra_features/sfml_stats/core/forecast_collector.py` | 52 | 165 | 22 |
| `extra_features/sfml_stats/core/heating.py` | 381 | 554 | 138 |
| `extra_features/solar_forecast_eai/insights.py` | 265 | 613 | 123 |
| `data/data_weather_precision.py` | 43 | 214 | 23 |

Actual parameters, globals, attributes, imported objects, SQL/payload schemas,
documentation, decorators/defaults/annotations, signatures and ordered closure
slots remain fixed. Names distinguish weather readings from values, percentage
from fractional confidence, power from energy, samples from counts, and heating
setpoints from measured temperatures. Actual generator scopes own their targets;
`.0`, lambda shadows and assignment-expression ownership remain protected.
Class bases, keywords and decorators are visited in the enclosing scope before
entering the class body, so nested classes retain their captured imports.

The full selected suite passes **203 methods in160.934seconds** against the .35
generation and106-module readable snapshot, before these six naming-only
publications. Their complete compiled trees remain exact; the separate owned
investigatory controls are not added to that test-method count. No full
integration or armor runtime is imported. Candidate, readability and controlled
behavior coverage remain distinct; full source recovery is unfinished.


## Readable Energy AI database, rule-based strategy and EMS

Root-authored source names **1,865 bindings across
304 actual scopes**, exposing **689
already-proved units**. It removes 63 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**115 modules / 5,671 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `extra_features/solar_forecast_eai/data/db_manager.py` | 237 | 416 | 90 |
| `forecast/forecast_rule_based_strategy.py` | 208 | 773 | 99 |
| `extra_features/sfml_stats/core/ems.py` | 244 | 676 | 115 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

The EMS names preserve the thirteen-slot nested flush capture layout. The rule-based strategy preserves its target-date capture and distinguishes weighted forecasts, adaptive weights and group mappings. Formatting initially added a trailing newline to one original docstring and failed the signature proof (207/208). Restore that exact value with an escaped string literal, format again and verify every docstring value. The rejected formatted source remains private evidence; the published source passes208/208. Actual SQL, model and payload fields remain fixed.

The previously passed 203-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Readable prediction, model promotion and GPM API

Root-authored source names **965 bindings across
216 actual scopes**, exposing **479
already-proved units**. It removes 243 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**118 modules / 6,150 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `ai/ai_predictor.py` | 179 | 481 | 75 |
| `extra_features/sfml_stats/api/gpm.py` | 164 | 294 | 60 |
| `ai/ai_model_promotion.py` | 136 | 190 | 81 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

The prediction source preserves heartbeat ownership and lease-loss versus external-cancellation routes, feature ordering, model field names, and residual allocations. Model promotion preserves twenty-four capture contracts. The GPM API names distinguish internal rows from customer-visible DTOs, CSV bytes, form fields and upload readers while preserving privacy schemas, BOM/profile/language handling and upload limits. The imports retain their actual modules and objects; only local aliases are renamed.

The previously passed 203-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Readable correction allocation, weather blending and physics calibration

Root-authored source names **863 bindings across
137 actual scopes**, exposing **370
already-proved units**. It removes 144 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**121 modules / 6,520 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `extra_features/sfml_stats/core/corrections.py` | 127 | 315 | 52 |
| `data/data_weather_expert_blender.py` | 122 | 234 | 40 |
| `physics/physics_calibrator.py` | 121 | 314 | 45 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

Correction allocation distinguishes decimal amounts from integer cents and preserves captured array order, allocation ties, audit hashes and transaction cleanup. Weather blending names distinguish cache timestamps and TTLs, expert objects and identifiers, cloud layers, normalized weights, rollback snapshots and snow-learning thresholds. Six original weather docstring values are restored after formatting. Physics calibration retains clamp parameters and bound defaults; two local aliases keep their actual imported objects.

The previously passed 203-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Readable GPM coordination, corrected energy and thermodynamics calibration

Root-authored source names **699 bindings across
124 actual scopes**, exposing **333
already-proved units**. It removes 54 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**124 modules / 6,853 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `extra_features/grid_price_monitor/coordinator.py` | 117 | 230 | 40 |
| `extra_features/sfml_stats/core/corrected_energy.py` | 115 | 280 | 45 |
| `extra_features/solar_forecast_eai/thermodynamics/thermodynamics_calibrator.py` | 101 | 189 | 39 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

GPM coordination preserves the license recheck callback captures, provider/cache fields, current-hour selection and retry cancellation. Corrected energy distinguishes raw versus corrected kWh, scaling bases, prices, cost units and read-only query rows; the public values capture stays fixed in all three generator children. Thermodynamics preserves sorting lambdas, actual time imports, engine U-factor restoration and heating/hot-water energy routing. Formatting preserves each original docstring value.

The previously passed 203-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Readable STATS shared API, tomorrow view and weather intelligence

Root-authored source names **559 bindings across
112 actual scopes**, exposing **296
already-proved units**. It removes 27 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**127 modules / 7,149 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `extra_features/sfml_stats/api/common.py` | 103 | 132 | 33 |
| `extra_features/sfml_stats/api/tomorrow.py` | 98 | 231 | 43 |
| `extra_features/solar_forecast_eai/weather_intelligence.py` | 95 | 196 | 36 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

The shared API names clarify release integrity paths, client addresses and networks, completed/clean evaluation counters, database fallback and forecast-versus-actual error denominators. Tomorrow views preserve the max-export and selected-day captures while distinguishing timeline quality, SOC, journal records and period totals. Weather intelligence preserves independent station-versus-recorder provenance, official-alert priority, warning evidence, trend thresholds and HTTP cancellation routes.

The previously passed 203-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Promoted asymmetric return joins

Unpacker **0.4.36** is promoted after fresh complete source generation for all274
unchanged payloads in1899.8seconds, with zero API-generation reuse. Its directed
false-arm/whole-expression folds and multiline layout recover both JSON count
readers and the timestamp adapter without changing their work. Raw coverage is
**13,929/14,291 /138 complete raw modules**. Frozen106-module and current124-module
overlays independently reach **14,273/14,291 /263 complete candidates**. Every
previous matching unit and complete module is retained: three gains, zero losses.
All274 sources compile. Comparison rules and native0.6.30 stay unchanged.

Black preserves every old and new match in the three targeted raw modules;
their other incomplete raw neighbors remain unclaimed. Main-branch CI passes219
unpacker cases (218passes, one existing expected failure), C++ builds and Actions
security. The separately named Regression Test workflow is skipped. The full
selected component suite passes206 methods in137.636seconds against .36 and the
121-module readable snapshot; later naming-only publications have complete
static proofs and separate owned controls.

**Eighteen units remain strict failures**: seventeen real coroutine-return
coverage gaps and the WeatherExpert duplicated terminal rethrow. Research views
and owned tests document them; no new comparator variance is applied or credited.
See the coroutine and terminal-layout reports. These failures and unfinished
readability work keep the overall recovery goal active.


## Readable weather actual tracking, data manager and Open-Meteo client

Root-authored source names **521 bindings across
104 actual scopes**, exposing **272
already-proved units**. It removes 63 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**130 modules / 7,421 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `data/data_weather_actual_tracker.py` | 92 | 258 | 45 |
| `data/data_manager.py` | 90 | 117 | 27 |
| `data/data_open_meteo_client.py` | 90 | 146 | 32 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

Actual-weather tracking preserves the freeze detector recent-values capture, compiler annotation/class captures, sensor constants and fog imports while distinguishing measurements, units and lifecycle state. The data manager keeps forecast locks, database interfaces, persisted profiles and optional join fields intact. Open-Meteo names distinguish provider-specific radiation, missing versus zero values, date coverage, endpoint failover and retries; shared rate-limit list identity, daylight-saving boundaries and cancellation routes remain unchanged.

The previously passed 206-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Readable Energy AI data manager and weather precision tracker

Root-authored source names **199 bindings across
46 actual scopes**, exposing **176
already-proved units**. It removes 72 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**132 modules / 7,597 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `extra_features/solar_forecast_eai/data/data_manager.py` | 88 | 102 | 26 |
| `extra_features/weather_fusion_ai/ai/precision_tracker.py` | 88 | 97 | 20 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

Energy AI data-manager names distinguish power in kW from legacy database column names; the original hour-zero truthiness fallback and stored schemas remain intact. The weather precision tracker retains cloud, combined, expert, hourly and seasonal capture ordering, including cutoff before self, and preserves optional imported symbols while naming their local bindings. Both complete modules retain docstring values, parameters, instructions and exception coverage after formatting.

The previously passed 206-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Readable JSON migration twins and Energy AI weather station adapter

Root-authored source names **1,089 bindings across
143 actual scopes**, exposing **365
already-proved units**. It removes 9 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**135 modules / 7,962 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `extra_features/solar_forecast_eai/data/json_migration.py` | 145 | 449 | 53 |
| `extra_features/solar_forecast_eai/weather_station_adapter.py` | 77 | 195 | 37 |
| `data/json_migration.py` | 143 | 445 | 53 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

Both JSON migration modules now distinguish persisted record types, sparse model indices, pending statements and transaction accounting, including generic _localN bindings restored by earlier reconstruction. Public lambda parameters and separately owned generator targets remain intact. The station adapter keeps Recorder imported symbols and eleven ordered captures intact. Root AST renaming is followed by the already-verified API .36 multiline-return layout pass before Black; full-module proof confirms both _count_entries owners and _state_timestamp retain exact instructions and exception coverage. Plain AST collapse remains a failing negative control. No comparator rule or original bytecode changed.

The previously passed 206-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Readable telemetry, consumer views, weather tracking and DEV-mode contracts

Root-authored source names **587 bindings across
123 actual scopes**, exposing **314
already-proved units**. It removes 90 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**139 modules / 8,276 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `physics/physics_calibration_telemetry.py` | 82 | 110 | 29 |
| `extra_features/sfml_stats/api/consumer_views.py` | 81 | 174 | 31 |
| `extra_features/solar_forecast_eai/data/data_weather_actual_tracker.py` | 81 | 254 | 40 |
| `core/core_dev_mode.py` | 70 | 49 | 23 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

Telemetry names keep the original stamp-learning and pruning boundaries, quantile parameters and capture routes. Consumer views preserve config/read/entity resolver ordering and actual Recorder and heating imports. Energy AI observation names distinguish rain counters/rates, fog confidence, per-group state and radiation versus production ratios; cancellation, notifications and SQL remain unchanged. Root-authored DEV-mode names expose canonical payloads, signatures, install hashes, expiry listeners and admin services; all original authorization and validation branches remain intact. A separate nonexecuting naming audit of the first 135 published modules finds no remaining opaque non-parameter bindings.

The previously passed 206-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Readable shadow learning, radiation ensemble, candidate shadowing and notifications

Root-authored source names **526 bindings across
116 actual scopes**, exposing **297
already-proved units**. It removes 90 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**143 modules / 8,573 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `data/data_shadow_pattern_learning.py` | 80 | 181 | 32 |
| `data/data_weather_radiation_ensemble.py` | 80 | 125 | 31 |
| `data/data_model_candidate_shadow.py` | 79 | 164 | 37 |
| `services/service_notification.py` | 58 | 56 | 16 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

Shadow-pattern learning preserves the singleton declaration and independently owned generators. Radiation ensemble names expose confidence residuals, provider alignment, source renormalization, radiation partition and Kalman bias without changing original calculations or gates. Candidate-shadow names distinguish serving and trial models and retained evaluation records. Root-authored notification names distinguish message sections, percentage values, weather measurements and snow-depth estimates while keeping original options, notification IDs, texts, dispatch order and exception behavior. Full proofs run after every formatting pass; zero additional corpus match credit is claimed.

The previously passed 206-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Readable service registry, Energy AI predictor and power-source collector

Root-authored source names **552 bindings across
86 actual scopes**, exposing **210
already-proved units**. It removes 81 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**146 modules / 8,783 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `services/service_registry.py` | 58 | 112 | 22 |
| `extra_features/solar_forecast_eai/ai/ai_predictor.py` | 76 | 222 | 33 |
| `extra_features/sfml_stats/power_sources_collector.py` | 76 | 218 | 31 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

Root-authored registry names distinguish service definitions, governed search results, retained trial candidates, operational forecast settings, retrospective predictions and quarantine-aware shadow backfill records. GridSearchOptimizer, call, detect_hardware and self retain ordered closure slots; DEV-only filtering and all original service gates remain intact. Energy AI predictor names retain training-hours/date and prediction-scale capture order and original feature/persistence schemas. Its first formatted proof rejected analyze_feature_importance: splitting a repeated-target tuple across lines changed POP_TOP into an additional STORE_FAST. Retaining the independently proved same-line assignment with a narrow fmt: skip comment restores all 76 instruction and exception proofs without changing comparison rules; the failing attempt is retained privately. Power-source collector names distinguish W from kWh and charge from discharge while preserving Recorder period-end/start ordering and actual imported symbols.

The previously passed 206-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Repeated-target tuple layout survives readable formatting

The Energy AI predictor's first readable formatting pass matched 75 of its 76
units. In `analyze_feature_importance`, wrapping the repeated-target unpacking
assignment changed one `POP_TOP` into `STORE_FAST`. The comparator rejected this
instruction difference; the failed attempt remains private and was not published.

The root formatter now retains a proved same-line repeated-target assignment
with a narrow `# fmt: skip` comment. The complete formatted predictor again
matches all 76 units, including exception coverage. This preserves the original
assignment and its discarded value; no comparison rule or bytecode is changed.

`recovery/tests/test_same_line_unpacking.py` reproduces the effect using owned
literal code. Its two tests reject line-split instructions and changed return
values, prove the kept layout, and compare normal, malformed-tuple, error and
cancellation outcomes. These witnesses do not execute the integration.

The full selected suite subsequently passed **208 methods** in **134.153 seconds**
against frozen API .36 exports and a **143-module / 8,573-unit** readable snapshot:
196 application methods and 12 source/coverage methods. Later naming batches
receive full static source proofs separately. This is selected behavior coverage,
not execution or proof of the entire integration. Machine-readable evidence is
in `recovery/same-line-unpacking.json` and `recovery/toolchain.json`.


## Readable weather coordinator, mobility, solar reader and Energy AI lifecycle

Root-authored source names **538 bindings across
117 actual scopes**, exposing **279
already-proved units**. It removes 117 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**150 modules / 9,062 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `extra_features/weather_fusion_ai/coordinator.py` | 72 | 130 | 22 |
| `extra_features/solar_forecast_eai/mobility.py` | 71 | 191 | 40 |
| `extra_features/sfml_stats/readers/solar_reader.py` | 68 | 152 | 32 |
| `extra_features/solar_forecast_eai/runtime.py` | 68 | 65 | 23 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

Weather coordination names expose zero versus missing precipitation, fog visibility, UTC stepping and confidence caps without changing corrections or cancellation. Mobility names distinguish telemetry units/freshness, demand modes, eligible charging intervals and allocation fractions while preserving daylight-saving behavior and the read-only advisory contract. Solar reader names retain hourly/panel-group schema captures and original conservative planning limits. Energy AI lifecycle names keep task supervision, listener removal ordering, timeout/cancellation, persistence and original license revalidation unchanged. All four root-formatted modules retain their full instruction and exception proofs.

The previously passed 208-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Readable drift, Tiny LSTM, Energy AI coordination and support reports

Root-authored source names **1,009 bindings across
164 actual scopes**, exposing **402
already-proved units**. It removes 126 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**156 modules / 9,464 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `ai/ai_drift_monitor.py` | 69 | 209 | 34 |
| `ai/ai_tiny_lstm.py` | 64 | 257 | 29 |
| `extra_features/solar_forecast_eai/coordinator.py` | 69 | 123 | 23 |
| `extra_features/solar_forecast_eai/weather_fusion_adapter.py` | 68 | 153 | 27 |
| `extra_features/solar_forecast_eai/forecast/forecast_rule_based_strategy.py` | 66 | 171 | 25 |
| `extra_features/sfml_stats/core/support_report.py` | 66 | 96 | 26 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

The drift and Tiny LSTM modules expose training partitions, source intervals, model state and numerical intermediates while keeping all original ML calculations, tensor shapes and persisted weight formats. Tiny LSTM retains ordered cls/source-interval/embargo/sample-index/sample-label cells and np/self helper captures. Energy AI coordination preserves peak persistence and listener routing; its weather adapter preserves provider, timezone, privacy and sanitization contracts. Rule-based strategy names distinguish config entries, predictions, training/calibration records and thermal/insights engines without changing serving or fallback decisions. Support-report names retain nonlocal kept-mask/used-byte routing, family filters, record grouping, redaction and byte budgets. Full root-formatted instruction and exception proofs run for every complete module.

The previously passed 208-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Readable morning forecasts, Energy AI daily summaries and billing views

Root-authored source names **564 bindings across
89 actual scopes**, exposing **204
already-proved units**. It removes 117 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**159 modules / 9,668 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `production/production_morning_routine.py` | 75 | 272 | 30 |
| `extra_features/solar_forecast_eai/data/data_daily_summaries.py` | 67 | 144 | 32 |
| `extra_features/sfml_stats/api/billing_views.py` | 62 | 148 | 27 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

Morning forecasting names retain ordered datetime/hour-midpoint captures, original lock/sunrise precedence, deduplication and hour-zero decisions, current-day candidate filtering, retry/cancellation and quarantine gates. Energy AI daily summaries keep residual energy distinct from exceptions and preserve mean-actual capture order, missing/zero observations, retained forecast/weather metrics and SQL payloads. Billing view names retain allocated fee, tariff, own-supply, export and import capture routing and original pricing/JSON/month validation. All complete root-formatted modules keep exact instruction and exception coverage; source-only naming earns zero new corpus matches.

The previously passed 208-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Readable charging, billing, daily evaluation, weather client and shadow detection

Root-authored source names **830 bindings across
143 actual scopes**, exposing **378
already-proved units**. It removes 90 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**165 modules / 10,046 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `extra_features/sfml_stats/api/smart_charging.py` | 60 | 89 | 22 |
| `data/data_daily_summaries.py` | 73 | 153 | 36 |
| `extra_features/solar_forecast_eai/data/data_open_meteo_client.py` | 60 | 96 | 19 |
| `extra_features/sfml_stats/core/billing.py` | 60 | 199 | 26 |
| `extra_features/solar_forecast_eai/automation.py` | 58 | 95 | 15 |
| `data/data_shadow_detection.py` | 67 | 198 | 25 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

Smart-charging API keeps the already-proved multiline forecast-price return through API .36 restoration and Black. Daily evaluation names retain clean exclusion versus total-yield distinctions, window/MAPE/R2 boundaries, weather normalization and month cutoffs. Open-Meteo names preserve zero versus missing radiation, provider arrays, cache/rate guards and original database/HTTP contracts. Billing names retain correction and dynamic-price capture routing, currency units, calendar decisions and real imports. Energy AI automation preserves all license/readiness/trial/passive gates and listener lifecycle. Shadow detection names preserve measured/estimated group ratios, sensor precedence, technical limits, frost ownership, peer persistence and transaction cancellation. Full whole-module formatting proofs protect original instructions, exception coverage, interfaces and schemas; no new corpus match credit is claimed.

The previously passed 208-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Readable production tracking, tariffs, weather learning and Energy AI models

Root-authored source names **1,258 bindings across
223 actual scopes**, exposing **568
already-proved units**. It removes 189 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**176 modules / 10,614 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `production/production_tracker.py` | 55 | 76 | 19 |
| `production/production_actual_resolver.py` | 47 | 103 | 15 |
| `extra_features/weather_fusion_ai/ai/weather_learner.py` | 53 | 83 | 15 |
| `extra_features/weather_fusion_ai/ai/pattern_detector.py` | 45 | 76 | 16 |
| `extra_features/sfml_stats/api/gpm_monthly.py` | 55 | 125 | 24 |
| `extra_features/sfml_stats/core/grid_import_split.py` | 55 | 82 | 24 |
| `extra_features/sfml_stats/core/tariff_manager.py` | 54 | 118 | 20 |
| `extra_features/sfml_stats/core/weather_energy.py` | 53 | 131 | 28 |
| `extra_features/solar_forecast_eai/thermodynamics/thermodynamics_engine.py` | 51 | 103 | 16 |
| `extra_features/solar_forecast_eai/ai/ai_tiny_lstm.py` | 50 | 236 | 22 |
| `extra_features/solar_forecast_eai/energy_context.py` | 50 | 125 | 24 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

Names expose production timing and recorder samples, tariff currency conversions and ordered fallback captures, grid split accounting, weather-energy regressions, seasonal weather learning, pattern detection, thermodynamic COP and sink calibration, TinyLSTM matrix flow, and weighted load priorities. Original public fields, imports, interfaces and ordered closure slots are retained. Owned investigative fixtures supplement the whole-module static instruction and exception proofs; this batch claims no new corpus matches or full integration runtime coverage.

The previously passed 208-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.


## Readable tariff correction, charging decisions, reader summaries and drift tracking

Root-authored source names **1,044 bindings across
195 actual scopes**, exposing **494
already-proved units**. It removes 216 non-docstring
empty artifacts. Full instruction and exception proofs pass after formatting;
every current public source/hash/subtree proof passes. Readable coverage reaches
**186 modules / 11,108 units**, with
**zero new corpus matches** and unchanged comparison rules.

| Module | Exact units | Named bindings | Actual scopes |
| --- | ---: | ---: | ---: |
| `extra_features/grid_price_monitor/tariffs/corrections.py` | 54 | 143 | 30 |
| `data/data_weather_pipeline_manager.py` | 44 | 63 | 19 |
| `extra_features/sfml_stats/core/smart_charging.py` | 47 | 156 | 16 |
| `extra_features/sfml_stats/api/export_views.py` | 52 | 172 | 22 |
| `extra_features/sfml_stats/core/heating_model.py` | 52 | 30 | 12 |
| `extra_features/sfml_stats/sfml_data_reader.py` | 52 | 109 | 21 |
| `extra_features/sfml_stats/readers/annual_forecast_reader.py` | 49 | 105 | 20 |
| `extra_features/solar_forecast_eai/data/data_shadow_pattern_learning.py` | 50 | 95 | 18 |
| `extra_features/solar_forecast_eai/ai/ai_drift_monitor.py` | 48 | 113 | 22 |
| `extra_features/solar_forecast_eai/consumption/consumption_tracker.py` | 46 | 58 | 15 |

Public parameters, imports, globals/attributes, schemas, documentation,
signatures/defaults/decorators/annotations, flags and ordered closure slots stay
fixed. Actual generator targets follow their own scopes; `.0`, lambda shadows
and parent-owned assignment expressions are protected. Names follow existing
data flow and contracts, without claiming original local spelling.

Tariff names distinguish invoice EUR from hourly ct/kWh and preserve weighted correction and rollback contracts. Charging names retain runway, discharge-efficiency and cheap-slot gates. Weather pipeline names distinguish horizon counts, refresh, startup retries and provider order. Reader and export names expose actual/predicted accumulators, calendar coverage, annual margins and CSV/ZIP fields. Heating preserves temperature delta, calibration and thermostat calculations. Energy AI names expose shadow learning EMA and group hydration, CUSUM drift and response expiry, consumption tracking thresholds and save/listener cleanup. Original schemas, interfaces, slot and capture order remain fixed; owned investigative fixtures complement complete static instruction and exception proofs without claiming full integration execution.

The previously passed 208-method selected suite remains separate from these
static naming proofs and owned investigatory controls. No full integration or
armor runtime is imported. Full source and readable recovery remain unfinished.
