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
100 readable whole modules (2,637 nested units) and passes 196 selected
test methods (193 application checks and three source-path checks). The full
corpus has 249 completely matched candidate modules and 14,256/14,291 matching
units; 35 units remain. Candidate fidelity
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
