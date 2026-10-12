# Static source recovery methodology

Powered by Solar Forecast ML by Zara-Toorox. Recovered derivatives retain the
repository's noncommercial LICENSE and attribution requirements.

## Completion criteria

This project has three separate requirements: recover the bytecode, produce
source whose compiled units match that bytecode under documented permitted
differences, and refactor that source for readability without changing its
behavior. Compilation, a clean decompiler log, matching function counts, and
passing a limited test suite do not establish completion.

The current repository contains **274 protected payload modules**: 99 in the
main integration plus 175 in bundled companion integrations. Runtime bootstrap
modules and already-readable source are inventoried separately; they are not
protected payloads. Earlier experiments recovered only the first 99 modules.
The full goal must not be reduced to that first batch. Static examination of
companion code does not install it, activate it, or change its license checks.

Keep the working integration intact until the replacement passes verification.
Development is on the `source-recovery` branch. Private input snapshots,
decrypted bytecode, sequence files, runtime binaries, and logs live outside
the Git repository. **Sequence headers contain AES material; do not commit or
print them.** Publish source, tests, tools, inventory hashes, and evidence only.

## Successful recovery path

1. Inventory protected payloads by the actual `__pyarmor__(...)` call and
   runtime import. Record input SHA-256 hashes and the source commit.
   `recovery/prepare_input.py` copies payloads and the five matching Linux
   x86-64 runtimes into a private working directory. Matching serial numbers
   do not imply identical runtimes or decryption keys.
2. Use the improved
   [unpacker fork](https://github.com/EiNSTeiN-/Pyarmor-Static-Unpack-1shot)
   to decrypt object trees statically and export ordinary marshal-format PYC.
   Use `--export-only` for the native executable: native AST support alone
   remains insufficient for these modern modules.
3. Read the exported code with CPython 3.14. The stable magic is `2b0e0d0a`.
   Validate every nested code object with `marshal` and `dis`. Neither importing
   the integration nor loading the vendor native runtime is needed.
4. Normalize specifically identified PyArmor entry/exit plumbing, select the
   ordinary slow branch of optimized `any`/`all`/`tuple` generator calls, and
   preserve real exception handlers. Record transformations, never suppress
   an unrecognized construct to obtain a green report.
5. Use the
   [modern decompiler fork](https://github.com/EiNSTeiN-/pycdc-modern)
   as a source candidate generator. Decode mixed constants and imports, then
   parse and compile the candidate without executing it. Structural recovery
   reports are evidence about syntax and warnings, not semantic equivalence.
6. Compile candidates with the same CPython minor version and compare each
   nested code unit. `oneshot/bytecode_fidelity.py` in the unpacker fork performs
   a fail-closed comparison; `recovery/audit_units.py` applies it to the entire
   manifest. Unmatched units, unsupported marker patterns, bad source, and
   missing files remain explicit failures.
7. Reconstruct failing units from their disassembly, preferably using small
   original regression fixtures to improve reusable tooling. Establish unit
   fidelity before renaming locals or restructuring expressions. Behavioral
   tests supplement the bytecode comparison; they do not replace it.
8. Refactor verified source with meaningful names, concise docstrings, readable
   control flow, and direct imports. Retain public APIs and license behavior.
   Recheck fidelity and behavioral tests after each change.

## Comparison whitelist and limits

The first verifier compares signatures, meaningful instructions, jump targets,
exception regions, and docstrings. It ignores filename/line information, cache
and padding instructions, constant/name table ordering, internal local names,
borrowed versus ordinary local-load selection, and fused local instructions.
It recognizes a pure jump to a shared return terminator left by PyArmor's
return epilogue. Keyword parameter names remain significant because callers
can use them; global and attribute names remain significant.
CPython's compiler-generated class `__firstlineno__` header value is also
location metadata. Only the exact class-header pattern is normalized. An
explicit assignment to that attribute later in the class body remains
significant; a negative regression checks that it cannot be hidden.
Numbered local definition bindings may be restored from the nested code
object's original function name. Only unambiguous, non-colliding local or
locally owned cell bindings qualify; parameter names, public bindings and
nonlocal cells are preserved. The transformation changes local metadata, not
instruction bytes or exception tables, then resolves closure captures by
position. Decorator applications and recursive self-captures have separate
regressions. Nested code names, bodies and callers still undergo comparison.

These are **normalized bytecode matches**, not claims of raw byte-for-byte
identity. Permitted differences must stay narrow and explicit. Do not normalize
different constants, tests, handler targets, calls, or missing statements into
matches. The verifier lowers scalar assert-marker loads and the import binding
contracts described below. Complex attribute/container markers remain
unsupported until independently tested lowering rules are added. The initial
coverage measure deliberately understates equivalence rather than accepting
unverified constructs. Whole-integration completion requires every unit and
every manifest entry to pass, followed by the readability cleanup audit.
Adjacent exception-table intervals may be joined only when their normalized
boundaries touch and their target, stack depth and lasti flag are identical.
This preserves the exact effective dispatch map. An uncovered meaningful
instruction, changed handler target or changed depth/lasti remains a failure;
an owned negative fixture checks a missing covered CALL.

An identity test immediately consumed by a truth jump may invert both its
`IS_OP` sense and the jump sense while retaining the same target and fallthrough.
Identity produces a builtin bool without invoking user equality or truth hooks.
The verifier canonicalizes only this coupled pair. Either inversion alone,
different destinations or intervening meaningful work remain significant.
Owned positive and negative cases include objects with raising truth/equality
methods. No general branch or exception-control-flow relaxation is allowed.

Simple import and from-import markers also have independently tested lowering
rules. Both the protected tuple importer and the ordinary interpreter import
sequence are compared as explicit module/relative-level/name/binding contracts.
Swapping imported aliases fails comparison. Complex attribute and container
marker expressions still require separate rules and remain failures.

## Discovered pitfalls

- Removing PyArmor's runtime exit handler can also remove the route into the
  compiler's coroutine/generator StopIteration handler. The error path is a
  runtime exit call, RERAISE, COPY/POP_EXCEPT/RERAISE cleanup, then the compiler
  intrinsic. Reconnect the body entries only after recognizing that entire
  chain. Do not merely drop its table entries. An owned callable-marker fixture
  demonstrates that the old normalization let StopIteration escape; the repair
  preserves RuntimeError and its cause. Coroutine, generator, async-generator
  and real nested-handler cases are compared against plain owned code.
- Frame-wide StopIteration routes must not extend a source-level handler's
  lexical extent. Following them made subsequent work become an erroneous
  success-only `else` and dropped seven solar-summary generators. Excluding
  these interpreter routes from lexical chain expansion restores every
  generator and the common continuation after handled exceptions. A small
  async fixture tests normal results and both handled error paths.
- PyArmor's entry hook replaces the standard initial RESUME instruction.
  After removing that hook, restore RESUME 0 at its known NOP slot after cell/
  closure setup and an optional generator-entry pair. Otherwise zero-argument
  super() can use the caller's frame. A new weather constructor comparison
  exposed this defect; a plain owned inheritance fixture confirms the repair.
- Extending an already merged comparison chain must share its last operand and
  keep its entire earlier prefix. Using the second operand as the shared value
  caused a three-link negated guard to evaluate its second call twice. An owned
  failing fixture reproduced the extra call; the corrected fixture preserves
  return values, short-circuit order and NaN behavior. A negative fixture also
  checks that explicitly separate comparisons retain their distinct calls.
- A decompiler may take a nested function's name from its renamed local
  binding instead of its surviving `co_name`. This made otherwise present
  helpers appear missing on both sides of the unit comparison. Restoring the
  local binding before source generation recovers the original definition
  name and updates its uses and closure captures together. This removed 220
  incorrectly named source units from the current audit, leaving 12 genuinely
  missing source units. It does not accept their unverified bodies: newly
  aligned helpers may still fail instruction, signature or handler checks.
- Do not assign the same readable name to distinct comprehension variables.
  CPython 3.14 inlines comprehensions and allocates separate local slots; a
  merged name can change saved/restored locals even when the output looks
  similar. The full-module check caught this during panel-group cleanup.
- Formatting can change the literal contents of multiline docstrings. Preserve
  those strings with local formatter exclusions, rather than ignoring their
  changed bytecode. Long escaped SQL literals can be rewritten as multiline
  strings, but confirm their decoded string contents and module bytecode are
  identical before accepting the readability edit.

- Python 3.14 jump bases include inline caches. Ignoring them changes branch
  targets and can turn valid control flow into misplaced `else`/`return` code.
- Python 3.14 `LOAD_GLOBAL`/`CALL` use different NULL-slot order than older
  releases. `CALL_FUNCTION_EX` also always has a kwargs slot, even without
  keyword arguments.
- Locals-plus slots include cells and free variables. Treating a slot outside
  `co_varnames` as a decimal literal can produce a bogus tuple callee such as
  `(0.2, 0.3)(...)` instead of `sum(...)`.
- Nested generators need the statement parser's closure and function-attribute
  handling. The old miniature comprehension simulator lost capture tuples.
  A fixture with two captured cells reproduced this failure; the updated
  Python 3.14 generator parser preserves both the nested call and its cells.
- Closure captures are bound by **position**, not `co_freevars` spelling.
  PyArmor sometimes renames the parent cell while leaving a nested child's
  old name. Recover actual capture names before descending into child code.
- An exit marker's four preceding instructions do not necessarily belong to
  PyArmor. A genuine `COPY`/`POP_EXCEPT`/`RERAISE` cleanup may immediately precede
  the exit handler. Removing its exception-table entries changes behavior.
  The normalizer now recognizes only adjacent padding and the exit handler's
  own `PUSH_EXC_INFO`; a dedicated regression checks preservation.
- A marker may have an `EXTENDED_ARG` prefix when a large code object has more
  than 255 constants. Handler recognition and return-jump rewriting must include
  those prefixes without consuming adjacent user cleanup. The dedicated wide
  marker fixture restored valid source output for four additional modules.
- Removing runtime-only statements can leave empty function/class suites.
  An explicit `pass` preserves their no-op body and valid Python syntax. This
  rule is restricted to genuinely empty suites after identified statements
  are removed; it does not replace unrecognized application logic.
- Future annotations serialize expression text. Writing stored `Any` as the
  source annotation `'Any'` adds another quote layer on recompilation. Parse
  stored annotation expressions when `from __future__ import annotations`
  applies; preserve explicitly quoted forward references.
- `compile()` inherits the caller's future flags unless `dont_inherit=True` is
  specified. A verifier that imports future annotations can otherwise create
  thousands of false signature mismatches in ordinary candidate modules.
  Candidate compilation now explicitly isolates compiler flags.
- The native export stage can leave an empty `.cdc.py` placeholder when source
  generation subsequently fails. Compiling that empty file succeeds but proves
  nothing. The full audit requires both a valid source-generation report and
  nonempty candidate source before counting a source as recovered.
- Larger async/database methods and context-manager cleanup remain difficult.
  Correctly retaining user handlers can expose decompiler failures previously
  hidden by excessive cleanup. Invalid candidates must be repaired rather
  than restoring that incorrect cleanup rule.
- One async transaction method split a nested handler-bearing `try` from its
  `else` and `finally` suites, emitting illegal source. The writer now rejoins
  only that specific structure, preserving every recovered statement. Its
  source compiles, but its cleanup-block order still differs from the reference
  bytecode and remains an explicit fidelity failure. Grammar repair alone is
  not semantic verification.
- None-led OR guards can send several failures to one return/continue block,
  while the final test jumps over that block on success. The old decoder
  inverted these into nested positive guards, accepting absent/negative cloud
  values and allowing incomplete geometry records to reach arithmetic. Small
  fixtures reproduced both errors. The new parser reconstructs the shared
  rejection condition and preserves copied middle operands in chained
  comparisons. Tests cover missing data, negative values, NaN, single call
  evaluation, and later constant returns that must not be absorbed.
- Do not replace `not (a <= b)` with `a > b`: NaN and custom rich comparisons
  make these expressions different. Recovery preserves the original negation.

## Reproducible commands

Build both native tools using the instructions in their forks. Use a CPython
3.14 virtual environment containing the unpacker's requirements. All output
paths below should be private directories outside this repository.

```bash
python3.14 recovery/prepare_input.py \
  --source custom_components/solar_forecast_ml --work-dir /private/recovery

python3.14 /tools/unpacker/oneshot/shot.py /private/recovery/input \
  -e /tools/unpacker/build/pyarmor-1shot \
  --modern-backend /tools/modern/target/debug/pycdc \
  --concurrent 2 --no-banner -o /private/recovery/recovered

python3.14 recovery/audit_units.py \
  --source custom_components/solar_forecast_ml --work-dir /private/recovery \
  --unpacker /tools/unpacker --verified-source recovery/verified_units
```

The audit exits nonzero whenever any module is unverified. A source-generation
process's zero exit code must never be interpreted as a fidelity pass.

## Progress ledger

- First batch: all 99 main-integration modules exported, 5,984 nested code
  objects readable; all 99 generated sources compiled. There was no bytecode
  equivalence proof. Twenty-nine modules had structural warnings.
- Full-scope work added the complete inventory, corrected exception cleanup,
  closure captures and annotation reconstruction, introduced static unit
  comparison, and rebuilt the source candidates. Exact coverage is recorded in the audit
  report from the current authoritative inputs rather than inferred from the
  first batch's successful syntax checks.
- Bytecode export is validated for the complete 274-module scope. The composite
  goal remains active because full source fidelity and readability requirements
  are still incomplete. No replacement has been deployed to Home Assistant as
  part of source recovery.
- A complete handwritten/readable `data/data_shadow_group_math.py` is now in
  `recovery/verified_units/`. All eight units, including its module body,
  annotation functions and generator, match. Its three application functions
  pass four test methods covering 409 bounded synthetic input cases against
  the original code. The test instantiates only these matched pure functions
  with controlled globals; it does not import an integration or native runtime.
- The latest full audit reports **13,343 matched units out of 14,291** and
  **63 complete module matches**. Sixty-three readable sources (1,415 code units) are published
  under `recovery/verified_units/`, with original and reconstructed SHA-256
  evidence in `recovery/verified-manifest.json`. All 63 published sources have undergone the
  readability refactor and pass complete module-tree comparisons. Current
  full-scope coverage and remaining failures are in `recovery/progress.json`.
- Readability cleanup now also covers the coordinator helpers and both Kalman
  filter copies: empty-string artifacts are removed and local names describe
  update age, innovation, uncertainty, gain, and adaptive noise. All three full
  module trees still match. Four additional behavior tests cover scheduling
  boundaries, required keys, summaries, existing merge mutation behavior, and
  filter updates/adaptation/resets across both copies. That initial suite had
  eight test methods. Selected classes are instantiated only after full static
  matches and with controlled globals; no integration module is imported.
- Earlier frame/handler work added 1,059 matching code units, followed by
  **233** from the annotation/exit checkpoint and 60 from the guard checkpoint.
  Chained-inline-filter work added **45**; the current shared-raise checkpoint
  adds **15** and preserves every
  prior matching unit, as recorded in `recovery/latest-transition.json`. The full
  source-fidelity and readability goals remain incomplete.
- Fifteen more module sources now use readable variable names and formatting,
  including the newly verified Energy AI feature engineer. Empty-string
  artifacts are removed, original docstrings and literal values are preserved,
  and the SQL schema's escaped text is readable SQL inside multiline literals.
  That checkpoint's selected behavior suite had **18 test methods**. Added checks cover
  allocation and rounding, radiation geometry, billing and meter overlap,
  consumer configuration and summaries, conservative forecast caps/freshness,
  weather evidence/provenance, feature extraction aliases and sequences, and
  electricity price calculations. The added methods perform **1,248 paired
  function comparisons** in addition to the earlier checks. Tests compare
  outputs, mutations and existing
  exception behavior with the original code. Known default values are attached
  explicitly to selected pure functions without executing their module bodies.
- Seven further sources are now complete and readable: demo persistence,
  aWATTar, fixed/demo/time-of-use tariffs, SFML Stats view registration and the
  Weather Fusion weather platform. All new imports keep their original modules
  and bindings; only internal aliases are made readable. Five added application
  test methods compare tariff windows/determinism, dependency calls, recorded
  persistence calls, weather properties and forecasts using explicit controlled
  dependencies. The application suite now has **23 methods**. No integration
  module, actual database or HTTP service is loaded.
- The frame-entry checkpoint used unpacker **0.4.7-einstein.1** at commit
  `6d3838b13ee907dd1466c5d089137d7666410b43`. Its 80 regression cases report
  79 passes and one documented expected failure in the legacy native backend;
  the optional modern context-manager path passes. The modern backend is now
  **0.6.5-einstein.1** at `f2b27217fc5c8a0f83abb3a7d889ea8318c433c1`, with
  15 owned behavior regressions and 26 existing Rust tests passing. The latest
  frame-entry follow-up audit retains the route-corrected corpus's coverage
  and has no lost matches. These checks prove selected verified modules;
  full source equivalence remains incomplete.
  Its pinned backend and native build also pass the unpacker fork's
  [GitHub source regressions](https://github.com/EiNSTeiN-/Pyarmor-Static-Unpack-1shot/actions/runs/38186126751)
  and [native build](https://github.com/EiNSTeiN-/Pyarmor-Static-Unpack-1shot/actions/runs/38186126864).
  The backend's [behavior workflow](https://github.com/EiNSTeiN-/pycdc-modern/actions/runs/38185294293)
  and [Linux/macOS/Windows builds, tests and clippy](https://github.com/EiNSTeiN-/pycdc-modern/actions/runs/38185294288)
  are also green.
- The current full inventory exports all 274 payloads and validates all 14,291
  nested code objects. Source generation produces compilable candidates for
  all 274 modules. The Energy AI database module's nested `try`/`else`/`finally`
  structure now compiles, but structural warnings and a transaction cleanup
  instruction-order mismatch remain. It has not passed fidelity verification.
  Compilable candidates still require full unit verification.

## Annotation and exit-tail checkpoint

This preceding checkpoint used unpacker **0.4.8-einstein.1**, commit
`6bdb2726dea52258f6ad9c517685339078472ba7`. It passes 86 of 87 owned regression
cases; the legacy native context-manager case remains a documented expected
failure. The optional modern path passes it. The backend is
**0.6.6-einstein.1**, commit `ed8c5d407e148ae160632f0363a05f2399836dd7`.
Its 17 owned Python 3.14 behavior cases and 15 library, 8 CLI and 11 integration
tests pass. The corresponding unpacker
[source regressions](https://github.com/EiNSTeiN-/Pyarmor-Static-Unpack-1shot/actions/runs/38187881244),
[native build](https://github.com/EiNSTeiN-/Pyarmor-Static-Unpack-1shot/actions/runs/38187881252),
backend [behavior regressions](https://github.com/EiNSTeiN-/pycdc-modern/actions/runs/38187749880)
and [build/test/clippy jobs](https://github.com/EiNSTeiN-/pycdc-modern/actions/runs/38187749800)
are green.

- A wide jump to an exit marker must lose its `EXTENDED_ARG` prefixes when it
  becomes an operandless return. Leaving an orphan prefix misled the source
  parser and hid six nested units. The correction preserves every instruction
  address and handler boundary. A fixture executes the normalized return.
- Future-annotated classes sometimes retain a compiler set cell whose only
  uses are `MAKE_CELL` and the recognized empty-set `STORE_DEREF` initializer.
  It is not a class attribute. The new source pass uses the original class's
  unique qualified name and metadata to remove that generated initializer,
  restoring the docstring's position. Comparison excludes exactly these three
  private setup instructions and this sole unused cell. Any read, capture,
  public binding, additional cell or different initializer prevents this
  allowance. Dataclass namespace tests and negative cases cover that boundary.
- Deferred class annotations use a real registration set and guarded entries
  in `__annotate__`. Preserve those by emitting a constant-folded `if True`
  field block. It emits registration operations with no runtime condition.
  Actual enclosing conditions and loops remain, and tests check annotation
  lookup and default-value call order. Removing that block would change the
  compiler's cells and annotation function and fail strict comparison.
- The backend can join a runtime initializer with a user assignment. Strip
  only its paired marker literal from the tuple; preserve all other values and
  stores. If multiple targets remain, their RHS expressions still all run
  before their target stores. Tests cover attribute write ordering and the
  constructor's full bytecode tree. All 72 unresolved marker cases are now
  resolved; three still have ordinary instruction differences and remain
  failures.

Seven additional complete modules are readable and verified: model reaction,
CSV-community tariff forwarding, STATS DEV-mode views, STATS EMS API, Energy AI
weather types, Weather Fusion forecast correction, and sun-position shadow
learning. Original literals, docstrings, external names and public interfaces
are retained. Four additional application methods bring the selected behavior
suite to **27**, checking tariff forwarding, seven model-reaction scenarios,
authorization/error responses and thirteen retained API-method paths.

One generic parser regression remained at that checkpoint: removing a wide exit prefix made
`GPMRevertCorrectionView.post` swallow its success response into an exception
region. Its previously verified body is retained as a readable isolated source
unit in `recovery/verified_functions/`, with hashes and scope in
`recovery/verified-functions.json`. This proves the method, not the containing
module. The audit first verifies this isolated unit against its original code,
then inserts that definition into the incomplete candidate and runs the full
module comparison. The corpus retains all previously matched units. Future
generation had to keep this retention step until the parser regression was fixed.
Do not classify the raw decompiler alone as having zero regressions.

Failures at that checkpoint comprised **1,832 instruction mismatches, 290 exception-table
mismatches, one signature mismatch, and six missing source units**. The whole
source-equivalence and readability goals remain incomplete; the live integration
is unchanged by this recovery work.

## Guard and meaningful-return checkpoint

Unpacker **0.4.9-einstein.1** at
`c59693869896ae438b2fafc4d2efaf9038ba5985` uses backend
**0.6.7-einstein.1** at `4d9924e8e9ac229a08119594de5bdf29fd90328d`.
Its 91 owned regression cases report 90 passes and the same documented legacy
native expected failure; the optional modern path passes. Twenty owned backend
behavior tests and the existing 34 Rust tests pass. The application suite has
32 passing methods, including 39 new correction-API scenarios and five isolated
pipeline scenarios. CI evidence and exact revisions are in `recovery/toolchain.json`.

- A masked sequence of NOPs before an exception return is not a value-building
  tail. The old tail matcher confused it with mainline padding and swallowed
  the success response. Require a meaningful independent value head before
  considering a repeated tail. Owned padded sequential-handler fixtures failed
  before this fix; genuine sunk-return and `else` fixtures still pass.
  `GPMRevertCorrectionView.post` now matches directly from the raw candidate.
  Its published isolated source remains useful evidence but is no longer
  retained by the corpus audit.
- Shared rejection guards may begin with ordinary truth tests and may return
  constructed objects, rather than only literals. The parser still checks the
  complete shared rejection/continuation graph. Its expression simulator now
  understands positional/keyword calls and closure-bearing generators by
  reusing the main generator parser. Chained comparisons and NaN polarity
  remain significant; fixtures compare call order and lazy consumption.
- A selected builtin slow path can contain a synthetic jump over padding.
  Remove that jump only when every intervening word was already masked as
  unreachable. Retain offsets, real work and handlers. This prevents a false
  `else` inside an enclosing boolean guard. Normalization remains idempotent;
  shadowed-callable fixtures verify the ordinary call's exact consumption.
- The identity/jump variance described above resolves a compiler spelling
  difference without ignoring a predicate or target. This is the only new
  comparison allowance in this checkpoint.

The complete STATS corrections API is now readable and verified (80 units).
Selected tests preserve capability/administrator gates, body limits, numeric
strings, same-origin checks, rate-window boundaries, error routes and callback
captures. No actual HTTP or database operation is run.

The independently verified `_start_pipeline_background` coroutine retains its
`self` capture, success/failure logs, handled errors and unhandled cancellation.
A constant-folded `return None if True else None` forces the same shared return
edge as the recovered bytecode; the condition emits no runtime test. A bare
return duplicates terminal returns and fails strict comparison. The source
comment explains this compiler-shape requirement. Its enclosing coordinator
is still incomplete: `retain_in_candidate: false` prevents inserting an unused
function merely to inflate matching-unit counts. Two isolated source units are
published separately from the 34 whole modules.

The full audit adds **60 matches and loses zero previous matches**, including
without isolated overrides. Remaining failures are **1,773 instruction
mismatches, 293 exception-table mismatches and three missing source units**.
All 274 bytecode trees validate and all 274 source candidates compile. Full
source equivalence and readability remain incomplete; the goal remains active.

## Reconstructed dashboard and initialization trees

Two further isolated definitions now pass full subtree comparisons:
`websocket_get_dashboard_data` (three units, including its two generators) and
`SolarForecastEAICoordinator._background_initialization` (five units, including
the pipeline coroutine, weekly callback, lambda and annotation function).
Both are readable and recorded in `recovery/verified-functions.json`. Four
isolated source files prove nine unique code units; the standalone pipeline
coroutine is also contained in its reconstructed parent, so it is counted once.

The generic decompiler misplaced shared work in exception suites and omitted
the setup-success branch. Reconstructing the original nested handlers restores
pipeline setup after a handled service error, conditional weather setup, queued
pipeline start, the common initialization continuation, prediction-cache fallback
and weekly retraining. Dashboard reconstruction restores both KPI generators,
accuracy/coverage filtering, truth-filtered price values and the enclosing error
handler. The original conditions and error behavior are preserved, including
the existing exclusion of zero price values; recovery is not a behavior fix.

Two callable loads also need the original compiler shape: a constant-folded
selection around `json.loads` or `dt_util.now` preserves ordinary attribute
loading followed by `PUSH_NULL`, rather than method-style `LOAD_ATTR`. Source
comments explain these expressions. No new verifier allowance was introduced.
The shared-return spelling likewise keeps the weekly callback's common return.

Eight dashboard scenarios and fourteen initialization scenarios compare outputs,
logs, import/read/lock/service call order, callbacks, cache values and queued
tasks. Only known owned imports are supplied to these selected code objects.
Failures exercise inner continuations, outer error reporting and cancellation.
The selected application suite now has **34 passing methods**.

The corpus audit verifies the two definition hashes and full subtrees before
inserting their bodies into incomplete candidate modules. It gains **six matches with
zero previous matches lost**, for **12,228 / 14,291**, and has **no missing source
units**. The unchanged raw decompiler still has three missing units and 12,222
matches; the increase requires these explicitly recorded reconstructions.
The audit still reports **1,770 instruction and 293 exception-table mismatches**.
All 274 candidates compile and the same 34 whole modules match. Full source
fidelity and readability remain incomplete; no recovered code is deployed.

Replacing an entire isolated definition initially dropped the dashboard's
registration decorators and lost its previously matching module body. The
audit now retains the enclosing definition's arguments, annotations, defaults,
decorators and type parameters and replaces only its verified body. The full
module body matches again. A subtree proof cannot authorize a changed caller
or registration contract; check the entire module after insertion.

## Proven shared-return source normalization

The latest unpacker is **0.4.10-einstein.1**; exact current commits and CI
evidence are recorded in `recovery/toolchain.json`. It keeps backend
**0.6.7-einstein.1** and passes 95 of 96 regression cases, with the same legacy
native expected failure. Five new owned tests cover shared returns, user truth
hooks and exceptions, nested closures, generators, coroutines, idempotency,
ambiguous definitions and negative constants/signatures/handler cases.

`preserve_shared_returns` tries an explicit constant-folded None return at the
end of an unmatched named function. It then recompiles and compares every
unit, accepts only directly matching function units, reverts unproven trials,
and verifies that no existing unit match is lost. It skips ambiguous names and
async generators. It cannot supply missing work or ignore a handler mismatch.
No new comparison allowance is introduced. Source comments and the recovery
report's `verified_shared_return_shapes` explain the resulting expressions.

A fresh normal CLI run processes all 274 inputs using this path. All bytecode
trees validate, all candidate sources compile, and the 206 structural-completeness
reports remain distinct from fidelity. It accepts **528** shared-return shapes
in raw generated source. Combined with the two proven body overrides, the
full audit gains **527 matches and loses zero**, for **12,755 / 14,291** and
**44 whole-module matches**. One raw callback improvement overlaps the earlier
manual reconstruction, so these are not counted twice. Raw generation alone
has **12,749** matching units and still lacks the three manually reconstructed
source units. The composite audit has none missing.

Five newly complete modules are now readable and published: named tariff
windows, battery statistics tracking, invoice archive, Energy AI capability
provider, and forecast regeneration. Empty-string artifacts are removed;
locals describe their roles; original strings, imports, public names,
annotations and license gates are preserved. Full code trees still match.
The selected application suite has **39 passing methods**, adding tariff
boundaries/overlaps/prices, capped battery integration and unavailable states,
invoice validation codes, forecast dependency/fallback paths and invalid/expired
capability gates. All operate with controlled owned dependencies.

Thirty-nine complete readable modules (745 units) and four isolated source
files are published. Five other newly verified candidate modules still need
readability cleanup: the Energy AI weather MLP, both physics calculators, sensor
data collector and repair-tool service. Their raw source remains private.
Remaining fidelity failures are **1,243 instruction mismatches and 293
exception-table mismatches**. The complete source-fidelity and readability
requirements remain unmet, and the goal remains active.

## Module namespace and terminal-value checkpoint

Unpacker **0.4.12-einstein.1**, commit
`05dc198cfc6771c083f5efc357ee0bc0b88abeed`, keeps backend **0.6.7-einstein.1**.
The unpacker suite reports **102 passes in 103 cases**, with the same legacy
native expected failure. Current CI is recorded in `recovery/toolchain.json`.
No new verifier normalization is introduced in this checkpoint.

Python 3.14 may implicitly create a module's `__conditional_annotations__` set
even with future-serialized annotations. The decompiler guesses an explicit
empty dictionary for that setup. Source compilation then recreates the proper
set and executes the guessed dictionary store afterward, overwriting a real
observable namespace binding. This is different from the inaccessible unused
class-cell allowance discussed earlier: module stores must remain significant.

`restore_module_annotation_binding` tries removal of the guessed source map,
accepts it only when the entire module unit then matches, and checks that every
old unit match survives. The compiler's set setup remains in both code trees.
Owned tests compare the set, annotation dictionary and other namespace values,
retain genuine explicit dictionary assignments, reject unproven literal/store
changes and check idempotency. This fixes **18 module units** and brings STATS
API context, tariff thresholds and EAI heat-pump source resolution to full
matches. All three are readable and published.

Terminal return sharing also affects explicit final values, including boolean
and numeric fallbacks through handled errors. The proven source pass now wraps
an existing final return as `return value if True else None`, retaining its exact
value expression. It still appends a None expression for an implicit return,
reverts unproven edits, skips async generators and ambiguous definitions, and
checks full signatures/instructions/dispatch and all previous matches. Owned
tests cover handled comparison failures and boolean values, keep already
matching call-valued conditional returns unchanged, compare returned object
identity/call order/errors, and reject changed values or branch predicates.

A probe of this extension adds **102** matching units with no regressions. A
fresh full CLI generation confirms **630** accepted shared-return shapes and
**18** accepted module binding repairs. All 274 sources compile, with the same
206 structural-completeness reports. The composite audit confirms **120 new
matches and zero previous matches lost** versus the preceding checkpoint:
**12,875 / 14,291**, **49 whole modules**, and no missing source units. Raw
generation has **12,869 matches** and still relies on the two proven body
overrides for the three originally omitted source units.

The astronomical sun gate and shared GPM forecast reader are also now complete,
readable and published. Application checks cover thirty elevation/time cases,
naive/aware comparison errors, the existing in-place yield mutation contract,
twenty-four forecast-reader scenarios, query parameters and time boundaries,
fifteen threshold validation cases, twelve day evaluations with 288 current-slot
decisions, and seven heat-pump mapping/status scenarios including wrong-unit
fallback and unknown/unavailable telemetry. The selected application suite now
has **43 passing methods**; dependencies are controlled stand-ins.

Forty-four whole readable modules (810 units) are published. Five other complete
candidate modules still need readability cleanup, and full source fidelity
still has **1,123 instruction and 293 exception-table mismatches**. The complete
objective remains unachieved and active. No recovered source has been deployed.

## Proven result-return exception scopes

Unpacker **0.4.13-einstein.1**, commit
`6828251470e9a495c2d909c1350fabc1b91cfb2d`, retains backend
**0.6.7-einstein.1** and the existing verification rules. Its regression suite
has **106 passes in 107 cases**, with the documented legacy native expected
failure. The modern recovery path covers that legacy case. The source-recovery,
native build and workflow-security checks pass on GitHub; links are recorded in
`recovery/toolchain.json`.

An otherwise matching decompiled function can put result construction after its
`try`/`except`, although the original exception handler also covers that work.
That changes observable behavior: conversion, indexing and formatting errors
escape the candidate instead of reaching the original fallback. A matching
instruction list alone cannot prove this code equivalent.

`restore_try_return_scope` considers only uniquely identified named functions
whose instructions and signatures already match and whose remaining failure is
the exception table. When a final return immediately follows a try statement
with handlers and without `else` or `finally`, it tentatively moves the return
into the try body. It accepts the edit only after the full function comparison
proves matching instructions, signatures and exception coverage, including
handler targets, depth and `lasti`. Unproven changes are reverted. A final
comparison ensures every previously matching unit remains a match. The pass
is idempotent and does not broaden the verifier's whitelist.

Owned fixtures compare normal and missing-key results, handled access and
conversion errors, unhandled errors, coroutine failure paths and cancellation.
Negative cases retain already correct outside returns and finally blocks, and
reject changed literals, calls and handler types. Some conditional returns
still fail because their precise protected return layout differs. Moving them
inside is insufficient; those failures remain visible rather than being counted
as recovered.

A fresh full CLI run accepts **219** return-scope repairs. All **274** exported
bytecode trees validate and all **274** source candidates compile. The same
**206** candidates report structural completeness, which is not semantic proof.
The composite audit reaches **13,094 / 14,291** matching units and **51** complete
module trees, with **219 new matches and zero prior matches lost** against the
terminal-return checkpoint. Raw generation has **13,088** matches. The existing
two proven body overrides still recover three otherwise omitted source units;
they are validated before insertion and preserve their callers' headers.

Four additional whole modules are now readable and published: the sensor data
collector (26 units), panel-group calculator (52), astronomy cache (38) and GPM
statistics store (34). Cleanup removes inert marker expressions and gives
intermediate quantities descriptive names while preserving public names,
docstrings, constants and verified control flow. The astronomy and statistics
modules became complete through the scope repair; the other two were complete
candidates from the preceding checkpoint.

Paired checks cover sensor parsing and availability, startup polling and
timeouts, panel calibration/snow/shares, astronomical geometry, cached hourly
and panel-group rows, production windows, statistics queries and retention
parameters, read/write failures, fallback values and cancellation. Every source
module receives its complete static proof before a selected class is built with
owned dependency stand-ins. The application suite has **47 passing methods**;
no integration module or native PyArmor runtime is imported.

Published readable sources now total **48 whole modules / 960 code units**.
Three complete candidates still need readability cleanup. Full fidelity still
has **1,123 instruction and 74 exception-table mismatches**. The full scope and
goal remain unchanged and active; no recovered code has been deployed.

## Empty protected expressions and lexical capture ordering

The empty-try extension is unpacker **0.4.14-einstein.1**, commit
`8b7321a06df4fde15a8aa599695ab3cb8af033e1`. Its **108** regression cases have
**107** passes and the same legacy native expected failure. A hoisted return
can leave only `pass` in its original try body. CPython may then optimize the
handler away, so the initial mismatch is reported as instructions rather than
only exception coverage. The same tentative return-scope repair now considers
that category too, while retaining full-unit proof and all earlier matches.
Owned attribute fixtures verify normal values, handled missing telemetry,
unhandled property errors and idempotency. A fresh CLI run adds **30** matches
and reaches **13,124** composite matches without regression.

Lexical capture repair is unpacker **0.4.15-einstein.1**, commit
`88c5d610297f031eaf7b49bdf6d88843ebded841`, with backend **0.6.7-einstein.1**
unchanged. The unpacker has **111 passes in 112 cases**, plus the same documented
legacy expected failure. GitHub source regressions, the native build and
workflow-security analysis pass. Current links are in `recovery/toolchain.json`.

CPython sorts free-variable names and nonparameter cell names lexically. PyArmor
can replace their names while retaining their original tuple positions. Merely
compiling those replacements can reorder a child's free slots and its parent's
capture tuple. Both parts matter: a child with matching numeric loads may still
receive the wrong values from a mismatching parent.

`restore_capture_name_order` derives lexical constraints from normalized
reference capture tuples and nonparameter cells. It changes only numbered
private bindings, excluding all public parameters and global names. Source
identifiers and reference metadata reserve names to prevent collisions. Fixed
names retain their order; incompatible constraints or unsafe name intervals
cause the trial to be skipped. A separate source tree receives deterministic
private spellings that make the compiler reproduce the required order.

The trial is accepted only when it adds fully matching units and keeps every
old unit match. Parent capture tuples, child slot operands, signatures, literal
values, control flow and exception tables remain significant. This is a source
repair, not a verifier permutation allowance. Owned fixtures compare captured
values, generators, nonlocal mutation and late bound attributes; they keep
public keyword parameters and globals unchanged. A swapped-capture negative
fixture still fails full-tree proof even when its child alone appears to match.
Repeated repair is idempotent.

A targeted probe adds **51** matches across twenty modules. The normal full CLI
pass additionally proves related cell-order cases outside that initial subset:
its report contains **59** newly matching capture-order units. It also records
**249** accepted return-scope repairs, **630** shared-return shapes and **18**
module annotation repairs. All **274** exported bytecode trees validate and
all **274** source candidates compile; **206** are structurally complete.
The composite audit reaches **13,183 / 14,291** and **54** complete module trees.
Against the preceding published result-scope checkpoint, it adds **89** matches
and loses **zero** previous matches. The two isolated body overrides remain
separate, statically proven evidence and preserve enclosing registrations.

A direct audit of unmodified CLI sources confirms **13,177** raw matches and
**53** complete raw module trees. Five additional composite matches come from
the two proven body overrides, and one from the already published complete
shadow-group source. Earlier raw-count labels inadvertently included that
shadow-group function: the shared-return, terminal-return and first result-scope
raw totals are corrected to **12,749**, **12,869** and **13,088**. Their composite totals and all
reported zero-loss transitions are unchanged.

The remaining buckets are **1,031 instruction** and **77 exception-table**
failures. Three units that previously failed instructions now reach exception
comparison and expose existing dispatch differences; no previous match was lost.
The full goal remains active and its 274-module scope is unchanged.

Three more complete modules are readable: the physics engine (49 units), the
Energy AI weather MLP (29), and the repair service (36). They preserve original
docstrings, public names, constants, gates and bytecode-significant shared
returns, remove inert markers and use descriptive numeric/storage variables.
Their complete trees still match. Paired physics checks cover 42 twilight,
validity and temperature combinations, weather fallbacks and geometry inference.
MLP checks use NumPy **2.5.4** with reset random seeds, compare weights and
gradients, train and restore persisted biases, check quality gates and propagate
cancellation. Repair checks cover seven dry/live/failure scenarios, positive
number guards, acknowledgments, quarantine, transaction ordering, audit writes
and hourly allocation. All database and Home Assistant dependencies are owned
stand-ins; no live data is accessed.

The application suite has **50 passing methods**. Published whole readable
sources total **51 modules / 1,074 units**. Three newly complete candidates—the
promotion policy, weather-energy API and weather-energy reader—still need
readability cleanup. Full source fidelity and readability remain incomplete.
No recovered source has been deployed to Home Assistant.

## Discarded unpack bindings and compound return values

Backend **0.6.8-einstein.1**, commit
`d219efafc8ef38c7d3910aa9f2c0ac62b8208a18`, restores surviving names for
discarded repeated unpack targets. CPython can elide earlier stores to the same
local as `POP_TOP`, keeping only the last store. PyArmor renames the remaining
binding. Guessing `_` for each discarded slot introduces a separate stored local
on recompilation and changes instruction/slot layout even when the unused value
does not visibly affect the result.

The parser reuses the next direct local store's spelling across only discarded
slots and padding within the remaining unpack frame. It stops at nested
unpacking, side effects and control flow. Actual underscore stores remain
intact. Owned generator fixtures require identical instruction bytes,
exception-table bytes and local metadata; behavior comparisons also cover
surviving values, empty inputs and wrong-arity/type failures. All **34** Rust
tests and **22** Python 3.14 backend behavior methods pass. A focused corpus
probe proves three previously mismatching weather collector units.

Unpacker **0.4.16-einstein.1**, commit
`fd0433c425c7a8eab037509859d42e450358af6f`, pins that backend in CI and adds
`restore_compound_return_shapes`. A structured candidate may split a conditional
value into a guard return and a final return, or retain one short-circuit arm as
empty nested if statements. The source pass tentatively reconstructs a single
conditional return or an OR expression from those existing tests and values.
It supplies no missing business literal or call, skips ambiguous definitions
and async generators, and accepts only full-unit matches while preserving every
earlier match. Unproven neighboring changes are reverted.

Owned checks compare nested dictionary values, missing keys, await failures and
cancellation. Rich comparisons return explicit truth objects so tests check
returned values and truth/call order rather than coercing everything to bool.
Changed values and legitimate empty branches remain rejected or unchanged.
The unpacker suite has **115 passes in 116 cases**, with the same documented
legacy native expected failure. Backend and unpacker GitHub checks pass; links
are recorded in `recovery/toolchain.json`.

The normal full CLI run records **11** proven compound-return shapes and **60**
capture-order repairs, alongside **249** return-scope, **630** shared-return and
**18** module-annotation repairs. All **274** exports validate and all **274**
source candidates compile; **206** report structural completeness. A direct
raw audit gives **13,193** matches and **54** complete module trees. The composite
audit reaches **13,199 / 14,291** and **55** complete module trees. It adds **16**
matches and loses **zero** previous matches. The existing two body overrides and
published shadow-group reconstruction account for the six extra composite
matches; neither evidence nor scope is counted twice.

Four more complete modules are readable and published: promotion policy
(20 units), modern weather-energy API (22), weather-energy reader (30), and main
weather types (15). Descriptive captured names retain the proven lexical order;
weather SQL is rendered in multiline literals whose values remain exact.
The repaired visibility predicate now retains both fog/clear directions.
Full static trees match after cleanup. Paired checks cover policy noise bands,
regimes, champion age and controlled relative imports; date/dev gates and API
callbacks; archive/snapshot queries, cache periods and database failures; and
visibility validity, provider priority, contradiction edges and transmission.
The application suite has **54 passing methods**. Relative imports use explicit
owned stand-ins; no integration module or PyArmor runtime is loaded.

Published readable source totals **55 whole modules / 1,161 code units**. Full
fidelity still has **1,015 instruction** and **77 exception-table** failures.
That checkpoint left a filtered-generator value-merge error unresolved:
`sum(row[1] or 0 for row in rows if row[1])` produced 5 on its test input,
while the candidate incorrectly produced 0. The following checkpoint repairs
that bug. The complete 274-module objective remains active and unfinished.
No recovered source has been deployed.

## Filtered generator values and attribute callable checkpoint

Backend **0.6.9-einstein.1**, commit
`e64d28965ae62b7421670f672fdb6d6c0f4ddb2c`, fixes two cooperating parser defects.
Python 3.14 places `NOT_TAKEN` between a value-preserving conditional jump and
the copied operand's discard. Checking only the immediately preceding opcode
mistakes that discard for an expression statement, preventing the `and`/`or`
merge. The bounded arm check now looks through padding. Separately, iteration
rejection is parsed as an inverted `if: continue` guard with the yield after it.
The generator rebuilder now records that guard as a filter and keeps the main
parser's yielded expression, rather than falling back to the legacy simulator.

Owned fixtures require identical generator instruction and exception bytes.
They cover false/None/true values, AND/OR chains, an identity-to-None filter,
descriptor getters and truth-test order. All **24** Python 3.14 backend behavior
methods and **34** Rust tests pass. A full CLI regeneration and audit gains
**12** units with **zero** earlier matches lost, reaching **13,211** composite
matches. The changes fix real value loss; no verifier allowance is added.

Unpacker **0.4.17-einstein.1**, commit
`cf7a9bae8555978552095aadfbaeb9a56cfaaf7f`, adds a separately proven source shape
for attribute callable values. The recovered code can contain `LOAD_ATTR` with
its method-call bit clear, followed by `PUSH_NULL`, while a direct source call
recompiles with the method optimization. `(target.method if True else None)(...)`
retains the original value load with no runtime condition. The pass reuses
existing attributes and arguments, checks complete unit fidelity, reverts
unproven attempts and requires every old match to survive. It skips ambiguous
scope mappings. Mixed call shapes and changed arguments/attributes are rejected;
the verifier keeps the optimization bit significant.

Four owned tests cover property lookup and argument/call ordering, bound method
capture and lazy iteration, coroutine handlers and cancellation, idempotency,
and negative cases. The unpacker has **119 passes in 120 cases**, with the same
documented legacy native expected failure. Backend and unpacker source/build
CI checks pass, with exact revisions and links in `recovery/toolchain.json`.

The final normal CLI checkpoint proves two attribute callable units and reaches
**13,213 / 14,291** composite matches and **55** complete module trees. Raw source
has **13,207** matches and **54** complete trees. The transition gains **14**
units in total and loses **zero** old matches. All **274** exports validate and
all **274** generated sources compile. The existing body overrides and the
shadow-group source account for the six composite-only matches, as before.
`recovery/latest-transition.json` identifies every new unit path.

Seven new units have separately published readable generator contexts: four
daily-total terms from production/consumption, actual-energy terms, coverage
milestone terms, and a captured numeric finite check. Only the nested generators
are verified; their wrapper definitions and containing application methods are
not claimed complete. `recovery/verified-generators.json` records exact scope,
candidate paths and hashes. They need no candidate override and are not counted
twice. Generator code also inherits `from __future__ import annotations` even
when it has no annotations itself; two contexts retain that flag explicitly
instead of weakening signature comparison.

Five selected application tests add **37** paired scenarios and bring the full
selected suite to **59 passing methods**. They check errors and getter/truth
order, separate coverage predicates, lazy captured calls, finite/NaN/infinite
inputs, and conversion of a getter's StopIteration into RuntimeError with its
cause intact. No integration module or vendor native runtime is loaded.
The previous **55 readable whole modules / 1,161 units** remain verified.
Full source fidelity still has **1,001 instruction** and **77 exception-table**
failures. The full 274-module goal remains active and unfinished; no recovered
source has been deployed to Home Assistant.

## Independent lookup guards and corpus-retention checkpoint

Backend **0.6.10-einstein.1**, commit
`0b6f432707f7a7a5167e1876d23350cfed71d920`, repairs a lookup method with two
independent rejection guards. The old parser bounded the first guard's success
suite by the second guard's back edge. It then emitted the return outside that
suite, so a rejected non-dictionary row could return an unbound local. An owned
ordinary fixture reproduces UnboundLocalError without PyArmor involvement.

The accepted rule requires one rejection back edge to the current for-loop
head, an unprotected code unit, assignment of a local in the success span, a
later independent None check of that same local, and a return of that local.
Inline comprehensions, nested while loops and other suites retain their existing
handling. The parser emits an explicit continue guard rather than inferring an
overlong success suite. Negation preserves the original comparison operator;
folding `<` into `>=` would change NaN and user comparison hooks. None-identity
guards retain their dedicated spelling. Owned checks cover invalid and missing
rows, false-valued coordinators, getter order, exact instruction/exception bytes,
NaN, custom comparison operators and truth conversion that must not run.
All **27** backend behavior methods and **34** Rust tests pass.

The broader initial trial is rejected evidence, not a published recovery
checkpoint. Applying this conversion to general guards lost **112** raw matches
while gaining 26, with protected-region failures especially visible. The small
owned fixture and green CI were insufficient to justify that change. The bounded
rule retains every baseline raw and composite match. The verifier's handler-map
rules are unchanged. Separate trials that move conversion/timezone returns into
empty try bodies still fail exact exception coverage; they remain unaccepted.

Unpacker **0.4.17-einstein.1**, commit
`5270c12f06938de52fd4970d5e2b9056fa19d2d7`, pins the accepted backend. Its source
normalization and native exporter are unchanged from the attribute-callable
checkpoint. **119 of 120** unpacker cases pass, with the same documented legacy
native expected failure. Current CI evidence is recorded in `recovery/toolchain.json`.

Source regeneration reuses the unchanged **274** verified full-CLI exports and
sequence headers, invoking the normal recover API for every module. Four private
processes complete this CPU-heavy pass in **117.1 seconds**; each worker checks
that its export SHA-256 still equals the original. This avoids repeated native
decryption while retaining full scope. Both source and input audits still run
for every module; copied source candidates are never counted as a regeneration.
The prior full native/CLI export checkpoint remains the bytecode evidence.

The final audit reaches **13,215 / 14,291** composite matches and **55** complete
module trees. Raw output has **13,209** matches and **54** complete trees. The
transition adds **two** lookup units, with **zero** lost matches in both reports:
the EMS coordinator lookup and the stats API's coordinator lookup. All **274**
exports validate and all **274** generated sources compile; structural
completeness remains 206 and must not be equated with source fidelity.

Both lookup units are also published as readable isolated functions. The EMS
class shell and both containing modules are not claimed complete. Each receives
full unit proof. Four application test methods cover **23** input/error scenarios,
including missing HASS, repeated context reads, false results, getter order and
unhandled errors. The generic parser now recovers both, so neither receives an
override or duplicate corpus count. There are now **six** isolated function
source files proving **eleven** distinct units; the earlier two retained body
overrides remain unchanged.
The full selected application suite passes **63 methods**, alongside the seven
readable generator units and **55 readable whole modules / 1,161 units**.

Remaining fidelity failures are **999 instruction** and **77 exception-table**
units. The complete 274-module objective remains active and unachieved.
No recovered source has been deployed to Home Assistant.

## Shared branch-return blocks and readable shadow contracts

Unpacker **0.4.18-einstein.1**, commit
`fe922e093306f408c6066e1931fee4c78a77cb76`, adds a source-shape repair for
shared return blocks. The backend remains **0.6.10-einstein.1** at
`0b6f432707f7a7a5167e1876d23350cfed71d920`. Python and native version banners
are now synchronized. All **126** unpacker cases run with the modern backend:
**125 pass**, plus the unchanged documented legacy native expected failure.

The [CPython 3.14.2 flow-graph optimizer](https://github.com/python/cpython/blob/v3.14.2/Python/flowgraph.c)
can inline short exit blocks with a four-instruction copy limit. Removing
runtime instrumentation can leave a shared call/return edge that ordinary
source recompilation duplicates. A single folded edge was insufficient for
some keyword-call and conditional-dictionary tails. The new pass tries one
through four constant-folded layers around existing return expressions.
It adds no runtime predicate and preserves all existing values and operations.
Each attempted function must match its complete recovered unit, including
signature, branches, nested units and exception dispatch. After failed
neighboring trials are reverted, every old match and accepted new match must
still survive. Nested definitions are collected independently and async
generators are skipped. Accepted paths appear in
`verified_shared_branch_return_shapes`. No verifier rule or handler-coverage
allowance changed.

Six new owned regression methods cover keyword-call descriptors and argument
order, chained rich comparisons and identity, dictionary evaluation, nested
closures, coroutine handlers and cancellation, idempotency and retained prior
matches. Wrong constants, keyword arguments and parameter contracts stay
failures. Separate application tests confirm the recovered formatter and
range-guard functions against their original selected code, including errors
and truth hooks that must not run.

All **274** source candidates were regenerated over the unchanged validated
exports from the preceding full native/CLI checkpoint. An interrupted first
run produced 153 complete reports. Generation resumed in batches of 50, 50
and 21 without changing the tools; this is recorded explicitly rather than
presented as one new native export run. Every input payload and export hash
remains unchanged. Raw and composite audits both add **45 matches**, with
**zero prior matches lost**. Raw generation reaches **13,254 / 14,291** and
**57** complete modules. The composite reaches **13,260 / 14,291** and
**58** complete modules. Existing verified overrides remain unchanged.

The three newly complete modules are published as readable whole sources:

- `core/core_shadow_forecast.py`: 16 matched units.
- `data/data_shadow_observation.py`: 27 matched units.
- `extra_features/grid_price_monitor/license/entitlements.py`: 38 matched units.

Internal locals are renamed descriptively while original public contracts,
documentation and algorithms are retained. Thirty-five unnecessary folded
layers are removed one at a time, accepting each removal only after the entire
module still matches. The source is formatted with `black==25.1.0`, then the
whole module trees are proved again. Remaining folded edges are compiler-shape
requirements, documented in comments; no PyArmor runtime artifacts are needed.
The entitlement constructor is tested in its complete module rather than
retained as a redundant isolated copy.

Two additional readable isolated functions cover `_gate_status_text` and
`_bounded_number`. Their containing diagnostic and energy modules remain
incomplete. They receive full unit proof, no body overrides and no duplicate
corpus count. There are now **eight** isolated source files proving **thirteen**
distinct units, plus the existing **seven** generator units.

The selected application suite passes **72 methods**. Five methods compare
**49** formatter, entitlement-constructor and range scenarios. Four additional
methods make **485** paired function comparisons covering shadow rejection
thresholds, seasonal blending, sensorless caps, calibrated physics, geometry
and weather/sensor boundaries, observation/learning routes and residuals.
These tests use controlled dependencies and known defaults; no integration
module or native runtime is imported. Complete readable coverage is now
**58 modules / 1,242 units**. Fidelity failures remain **954 instruction** and
**77 exception-table** units, so the complete objective stays active and
unachieved. The recovered source remains in the development branch.

## Protected return evaluation and bounded dispatch variance

Unpacker **0.4.19-einstein.1**, commit
`9e637321d73afb77d7186245ae968d228b7bb828`, retains backend
**0.6.10-einstein.1** at `0b6f432707f7a7a5167e1876d23350cfed71d920`.
All **134** unpacker cases run with the modern backend: **133 pass**, with
the same documented legacy native expected failure. Six additional owned
bytecode-fixture tests cover the dispatch rule below, and two nested source
fixtures extend the existing return-scope tests. The complete selected
application suite passes **77 methods**.

The verifier already compares a jump to a bare `RETURN_VALUE` as that return,
provided its destination has no intervening work. A leftover difference was
handler coverage over the original jump itself. The narrow added allowance
removes only a forward alias jump's own non-`lasti` dispatch slot. It does not
remove coverage over an actual return, a call, cleanup, or any other evaluated
instruction. Backward jumps and all `lasti` routes retain their original
coverage. Python's [opcode documentation](https://docs.python.org/3.14/library/dis.html#opcode-JUMP_FORWARD)
distinguishes the forward counter adjustment from backward interrupt checks.
The ordinary forward alias performs no fallible user operation. Existing
scope, stack-depth and handler comparisons remain strict over evaluations.

Owned fixtures replace one return with a forward jump to an existing bare
return, without moving instruction addresses, and protect just that jump's
slot in addition to the original body. Original and candidate outputs retain
object identity, callback and truth order, handled/unhandled errors,
generator return values and StopIteration routes, and coroutine cancellation.
Negative tests reject real-return coverage changes, changed call coverage,
handler types, remaining protected stack depths, constants, cleanup flags,
backward aliases and jumps to intervening work. This is an explicit comparison
variance; it must not be presented as a source-only repair or expanded to cover
other gaps in meaningful exception dispatch.

An initial private probe limited to jumps converted by the runtime normalizer
did not repair these functions. Their remaining edges were jumps to an actual
bare return and reached the separate canonical alias rule. The exported and
normalized bytecode is unchanged by this comparison adjustment. Another
private inspection found 20 capture-related failures; scoped capture-name
constraints remain an investigation, with no new capture repair published.

Source reconstruction also extends `restore_try_return_scope` from the outer
function tail to statement-block tails inside conditionals, loops and handlers.
It moves only an existing return immediately after a try with handlers and
without `else` or `finally` into that try body. It never supplies a missing
call, value or predicate. Nested definitions are handled independently. Every
whole function must pass static proof before any move is kept, unsuccessful
edits are reversed in order, and all earlier matches must survive. Owned row
fixtures check key/index fallback order, getter failures, missing keys,
unhandled errors and repeated repair idempotency; loop and nested-definition
fixtures also reject different key expressions.

All **274** source candidates are regenerated through the recovery API over
unchanged verified full-CLI exports in six batches: 50, 50, 50, 50, 50 and 24.
The tools stay fixed between batches and every input/export hash is retained.
Before regeneration, unchanged old sources are audited in a separate private
checkpoint under the new comparison rule. This adds **one** match with zero
old matches lost. New source adds **22** more, also without losing a prior
match. Raw and composite totals both therefore gain **23**. Raw generation
reaches **13,277 / 14,291** with **59** complete modules; composite source
reaches **13,283 / 14,291** with **60** complete modules. The two existing
verified body overrides are unchanged. The new whole-module matches are
`extra_features/sfml_stats/core/energy_efficiency.py` and
`services/service_daily_briefing.py`; their complete readable publication
remains to be done.

Five more readable isolated functions are published in four compilation
contexts: feature-engineer `_safe_float`, efficiency `_number`, both statistics
`_cell` copies, and weather `_safe_zone`. The shared row source receives
independent proof against each original module. The class shell provides only
compilation context. All five units match directly in generic reconstruction,
so none receives an override or duplicate count. Original public names,
annotation flags and the feature method's documentation are retained. Five
application methods make **82** paired comparisons, including None/false
values, custom conversion hooks, nonfinite numbers, default identity, row
access order, BaseException propagation, timezone truth/constructor errors
and fallback getter boundaries. Actual unhandled error types are preserved.

Readable whole coverage remains **58 modules / 1,242 units**. Isolated source
coverage is now **12 files / 18 distinct units**, alongside seven generator
units. Remaining failures are **934 instruction** and **74 exception-table**
units. The 274-module goal stays active and unachieved; recovered source stays
on the development branch.

## Complete readable efficiency and daily-briefing modules

The two complete matches from the preceding checkpoint are now published as
readable whole modules: `extra_features/sfml_stats/core/energy_efficiency.py`
has **31** matched units, and `services/service_daily_briefing.py` has **51**.
This adds **82** readable whole-module units. Internal locals, exception
bindings and the imported cache-manager binding have descriptive names.
Public names, metadata, documentation, thresholds, query values, message
strings and calculation/notification order remain intact.

Twelve empty-string or pass artifacts are removed only after whole-module
proof. Three unnecessary folded-return layers are pruned one at a time with
the same gate. Required folded compiler edges and the legitimate ignored
astronomy-formatting exception remain. The schema SQL is rendered as readable
triple-quoted text with exactly the original values, and the redundant
cache-manager import alias is removed. The files are formatted with the
existing `black==25.1.0` tool, then their complete trees are proved again.
No comparison allowance, backend revision or export changes in this pass.

Four efficiency methods compare direct sample accumulation, conversion
plausibility, PV/battery/mixed classification, charge and unknown states,
sample-gap bounds, hourly segmentation, coverage rejection, grace-period
finalization, sorting/duplicate timestamps and display-summary availability
and quality. They cover 20 sample trajectories, 12 window/splitting scenarios,
eight aggregate sequences and 65 summary/error combinations. These tests
decorate the statically verified classes using the real stdlib
`dataclasses.dataclass(slots=True)` with an owned temporary module context;
they do not replace the dataclass constructor or slots behavior with a fake.
Selected helper globals are linked explicitly, with no integration module
body executed.

Seven briefing methods compare localized weather/quote/closing text over
2,016 paired function calls, 14 clock/lock/sunrise scenarios, eight database
reader scenarios, four astronomy import/cache scenarios, six exclusion/shadow
scenarios, 12 complete renderer scenarios and 16 notification scenarios.
Exact query parameters and order, dictionary payloads, logs, false/missing
values, lookup errors, handled failures and cancellation are compared.
An optional builtins override on the existing class test helper supplies
only an owned relative-import implementation. Default class-test behavior
is unchanged. No actual database, astronomy integration or notification
service is contacted, and the selected astronomy success case is asserted
explicitly so a swallowed import failure cannot masquerade as a pass.

The existing `_number` tests now take their function from the complete
efficiency module. Its redundant isolated source and manifest entry are
retired. This is a change of publication scope, not a lost unit or a new corpus
match: the full source tree proves the function along with its caller and
module declarations. Other isolated source contexts remain necessary.

The complete selected application suite passes **88 methods**. A new full
274-module composite audit retains **13,283 / 14,291** and **60** complete
module matches, with every original input unchanged and zero vendor module
imports. Readable whole publication now covers **60 modules / 1,324 units**;
isolated publication covers **11 files / 17 distinct units**, plus seven
generator units. Remaining **934 instruction** and **74 exception-table**
failures keep the full goal active and unachieved. The next fidelity repairs
must retain the complete scope rather than treating these readable modules
as a smaller substitute objective.

## Chained inline-comprehension accepting edges

Modern backend **0.6.11-einstein.1**, commit
`9f37929a486a26f317b787623ab2e5854d3dd66b`, repairs early filter polarity
in chained inline comprehensions. Unpacker **0.4.19-einstein.1**, at commit
`131c0b04c7f5f91a84d9d5cfe22a50002db4dd85`, now pins that backend and adds an
end-to-end regression. Its normalization and comparison rules are unchanged.

[PEP 709](https://peps.python.org/pep-0709/) describes inline list, set and
dictionary comprehensions and their saved/restored locals. The
[CPython conditional-jump documentation](https://docs.python.org/3.14/library/dis.html#opcode-POP_JUMP_IF_FALSE)
defines the branch's target and fall-through behavior. In the owned 3.14
fixtures, a successful early guard jumps forward into the next guard, while
the fall-through skips the item. The old parser searched from the target for
an append; encountering the next guard's skip first made it invert the early
guard. This turned `isinstance(value, str)` into its negation and equality
into inequality in source candidates.

For inline comprehensions on Python 3.12+, the new recognition requires a
forward target strictly inside the active comprehension, with only padding
and exactly one unconditional backward jump between the guard and target.
That jump must return to the current comprehension's innermost FOR_ITER.
This exact skip path identifies the other edge as accepting the current guard,
even when later guards remain. Classic skip layouts and general forward
regions retain their previous handling. No test, value, rich-comparison
operator, call, handler or verifier slot is invented or ignored.

Two backend methods cover **14 owned fixture definitions / 52 input cases**.
Twelve variants combine list/set/dictionary comprehensions, positive/negative
guards and conjunctions versus successive filters. Two three-guard fixtures
compare equality/inequality hooks, readiness/finalization calls, short-circuit
order and errors. Exact function instruction and exception bytes are compared,
along with returned values and call traces. Before the repair, the new tests
fail on the first guard's polarity; afterward all **29** backend behavior
methods and **34** existing Rust tests pass. The unpacker adds **12** matching
complete fixture trees and value checks; all **135** cases run, with **134**
passes and the unchanged single documented legacy native expected failure.

All **274** candidates are regenerated from unchanged verified full-CLI
exports in six batches of 50, 50, 50, 50, 50 and 24 with fixed tools. Raw
and composite audits each gain **45 matching units** and lose **zero prior
matches**. Raw generation reaches **13,322 / 14,291** with **59** complete
modules; composite recovery reaches **13,328 / 14,291** with **60**. No new
whole module becomes complete in this pass. Existing readable sources,
isolated body overrides and exports remain unchanged. The transition records
all gained unit paths and the absence of a new comparison allowance.

Two newly matching functions have readable isolated source:
`_csv_headers` and `EMSManager._hour_points_from`. Original names, signatures,
annotations, duplicate values, repeated getter/strip calls and integer checks
remain intact. Four application methods compare **50 paired scenarios**:
18 CSV-header and 32 EMS-hour cases, including invalid input types, blank and
duplicate headers, custom string/truth hooks, integer/bool/float boundaries,
separate get/index reads, rich-comparison hooks and errors. Their complete
units are proved after formatting; their containing API/EMS modules remain
incomplete. Both are already recovered by generic generation, so neither
receives an override or duplicate corpus count.

All **92** selected application methods pass. Readable whole coverage remains
**60 modules / 1,324 units**; isolated source now covers **13 files / 19 distinct
units**, alongside seven generator units. Remaining **889 instruction** and
**74 exception-table** failures leave **963** unverified units. The full
274-module source and readability objective remains active and unachieved.
Recovered source remains on the development branch without deployment.

## Shared raises, comparison cleanup and formatted messages

Modern backend **0.6.12-einstein.1**, commit
`22c069c9f3a105d75b3f9ccbbb6b0877256e553b`, extends the existing shared
rejection-block recognizer to raises. Unpacker **0.4.19-einstein.1**, commit
`96783d9b732fd07f735a8e3c07e74cf58431482a`, pins that backend and adds a
whole-tree end-to-end regression. No normalization or comparison rule changes.

The [CPython raise-opcode contract](https://docs.python.org/3.14/library/dis.html#opcode-RAISE_VARARGS)
uses one operand for an exception and two for an exception plus explicit cause.
The parser now reconstructs that exact straight expression region and stack
count, retaining evaluation order. Bare reraises, stores, cleanup and general
branches remain unsupported by this recognizer. Existing literal and
constructed returns retain their handling.

A negated comparison chain followed by another predicate also has a success
jump over its failed-link POP_TOP/JUMP_FORWARD cleanup. The recognizer skips
only the complete trampoline back to this same rejection, after proving the
comparison-chain merge and an empty carried stack. Walking the cleanup on the
success path lost the later predicate or disturbed the copied middle operand.
The original comparisons and the single evaluation of that operand remain.
Many validation raises construct formatted messages. Their value simulator
now uses the main parser's f-string conversion/specification model for the
known 3.14 operations, with exact stack consumption. It rejects unexpected
BUILD_STRING parts rather than supplying an invented conversion.

Four new backend methods cover **seven owned fixture definitions / 312 paired
input scenarios**. They compare exact instruction and exception bytes,
predicate and constructor errors, NaN and rich-comparison hooks, conversion
and dynamic-format hooks, explicit cause order and exception metadata.
The unpacker adds a complete source-tree comparison and 12 end-to-end input
scenarios. All **33** backend behavior methods and **34** existing Rust tests
pass. All **136** unpacker cases run: **135** pass, alongside the unchanged
single documented legacy native expected failure.

All **274** source candidates are regenerated from unchanged verified
full-CLI exports in fixed-tool batches of 50, 50, 50, 50, 50 and 24. Raw and
composite audits each add **15 matches**, with **zero prior matches lost**.
Raw generation reaches **13,337 / 14,291** and **62** complete modules;
composite recovery reaches **13,343 / 14,291** and **63**. All original payload
and export hashes remain unchanged. No comparison allowance or new retained
body override contributes to these gains.

Three newly complete modules are published with descriptive internal names:
both `license/codec.py` copies (10 units each) and
`extra_features/sfml_stats/api/heating.py` (71 units). Their complete trees
match after `black==25.1.0` formatting. Original docs, validation strings,
signatures, defaults, imports, routes, registration order, decorators,
authorization and privacy behavior remain intact. This adds **91** readable
whole-module units without duplicate corpus counts.

Four codec methods compare owned payloads and policies: 22 canonical-JSON,
146 encoding/decoding, 181 composition and 11 policy/format scenarios. The
GPM codec has an optional prefix override and module-policy reads; EAI uses
imported constants and a two-argument composition API. Preserve each original
contract. A test harness that omits a controlled global can make both sides
raise the same irrelevant error; concrete successful outputs and intended
error assertions are needed in addition to paired results. Neither codec
constructs or submits a real license in these checks.

Five heating-API methods compare **382 paired scenarios**: 288 authorization
and run paths, 11 body/UTF-8/size/error cases, seven room-permission cases,
52 comfort-temperature validation/rounding/update cases and 24 status/privacy/
registration cases. They include unhandled failures and cancellation, exact
error statuses and logs, each thermostat's permission check, unchanged
temperature short-circuits and untouched private source data. Selected verified
classes/functions execute with owned HTTP-view, request, manager and policy
stand-ins. No integration body, HTTP server, license service or actuator is
used. Decorator registration is covered statically; stand-in execution is not
a claim of full Home Assistant middleware behavior.

The full selected application suite passes **101 methods**. A final composite
audit after the refactors retains the same count and complete-module scope.
Readable whole coverage is **63 modules / 1,415 units**; isolated coverage
remains **13 files / 19 distinct units**, plus seven generator units. Remaining
**874 instruction** and **74 exception-table** failures leave **948** unverified
units. The complete 274-module source/readability goal remains active and
unachieved. Recovered source stays on the development branch.


## Padded protected-value returns

Backend **0.6.13-einstein.1**, commit
`c5cef89401ef69f050a549858050742053bfacc3`, fixes a calculated value
stranded on the parser stack at a shortened protected range. Unpacker
**0.4.19-einstein.1**, commit
`ab70795494120b8ff12826ea46e831e0a7c25cc0`, pins it and verifies native
export through complete modern source comparison. No normalization or
comparison rules change.

The existing tail recognizer proves a span containing only padding and one
unwalked return, with no with protocol. In CPython 3.14 a RETURN_VALUE in that
span consumes the value already calculated by the protected body. The parser
previously supplied a bare return because a new region walk lacked that
value. The repair additionally requires **exactly one live expression** on
the stack. It consumes that expression once, preserving call/getter order,
return values and handler scope. Constant/bare returns and other stack or
control-flow shapes retain their handling; this does not invent a new value.

Two backend methods cover **five owned definitions / 435 paired scenarios**:
call, attribute, tuple and dictionary expressions, three padding widths,
false/None values, fallible truth conversion, handled calls/getters, fallback
errors, base exceptions and an explicit bare return's side effect. The owned
padding utility adjusts all jumps and precise exception boundaries and is
also reused by the earlier sequential-handler fixture. The unpacker adds
**two definitions / 72 end-to-end scenarios**, comparing complete units
before execution. All **35** backend behavior methods and **34** Rust tests
pass. All **137** unpacker cases run: **136** pass and the existing single
legacy native expected failure remains documented.

All **274** candidates regenerate from the unchanged full-CLI exports in six
fixed-tool batches. The final source and binary hashes are recorded. Raw and
composite audits each gain **15 units** with **zero previous matches lost**.
Raw generation reaches **13,352 / 14,291** with **63** complete modules;
composite recovery reaches **13,358 / 14,291** with **64**. This completes
`core/core_helpers.py`. No new comparison variance, retained body override or
isolated-source insertion contributes to the gain.

The full 28-unit datetime module is published with descriptive internal
names and its original public APIs and docs. Nineteen empty-string/pass
artifacts are removed, each after whole-module proof; formatting retains the
complete match. Seven new tests compare **159 paired scenarios** across
local timezone lookup, controlled HA delegation, errors and warning logs,
naive/aware conversion, DST, ISO parsing, day boundaries and the owned
relative import of WINTER_MONTHS. Existing edge cases, including an HA default
zone returning None, are preserved rather than silently corrected. These are
selected utility tests with stand-ins, not live HA integration execution.

Two isolated readable source files additionally prove three distinct units:
both tariff default_local_tz copies and the recursive json_ready helper.
Six application methods compare **85 paired scenarios** with real stdlib
zones and owned clock/conversion/mapping hooks. They check fallible truth,
constructor/call/getter order, exactly handled errors, UTC fallback, nested
conversions, nonfinite scalars, array/item fallbacks, raw-byte rejection and
iteration/key-conversion errors. Concrete successful values accompany paired
outcomes. Their original signatures and JSON validation behavior remain;
containing tariff/database modules are still incomplete. Each already matches
in generic generation and receives no override or duplicate corpus count.

The complete application suite passes **114 methods**. A composite audit after
readability changes retains all **13,358** matches. Readable whole coverage is
**64 modules / 1,443 units**; isolated coverage is **15 files / 22 distinct
units**, alongside seven generator units. Remaining **859 instruction** and
**74 exception-table** failures leave **933** units unverified. The full
274-module source/readability objective remains active and unachieved.

A separate diagnostic of coroutine conditional returns showed that moving a
return into its apparent try suite can repair the value operations while
still leaving a compiler-frame handler difference at a real return slot.
That incomplete trial is not retained. Do not erase real-return or meaningful
call/cleanup coverage to accept such cases; they require independent source
or runtime-plumbing evidence before a further repair.


## Reuniting shared exception-handler fragments

Unpacker **0.4.20-einstein.1**, commit
`f36fe479cbf68cb27b064c8c2e4f54c3ca9ab85f`, adds a source reconstruction
pass. Backend **0.6.13-einstein.1**, commit
`c5cef89401ef69f050a549858050742053bfacc3`, stays unchanged and pinned.
Normalization and comparison rules remain unchanged too.

Early returns split an exception table into protected fragments sharing one
lexical handler. The backend can emit those as consecutive try statements,
copying the same handlers. A source trial concatenates only adjacent try
bodies with exactly identical handler ASTs and no else/finally. Intervening
work and differing suites are not guessed. Inner statement suites are handled
before enclosing concatenations; nested definitions receive independent
trials. Unambiguous function scopes are tested against the complete original
instruction and exception-dispatch maps. The trial is kept only if the full
unit matches and every previous matching unit survives. Failed neighboring
trials are reverted before the final retention check. Existing matched
functions are never rewritten merely because their handlers look alike.

An unconstrained diagnostic found 70 potential gains but also five losses in
already verified functions. The proof-gated implementation rejects those
losses. This is evidence that identical handler text alone is insufficient:
a real sequence of independent tries can continue to later work after its
first handler, while one combined try would skip it. Do not merge based on
syntax or a few successful inputs alone.

Five new owned reconstruction tests compare sync return/call order, handled
and unhandled exceptions, coroutines and cancellation, nested definitions,
prior-match retention, wrong literals/calls, handler differences and
else/finally/intervening work. The behavior loops include 30 sync, 20 async
and four nested paired scenarios. A whole-tree native-export/modern property
fixture adds 40 getter, comparison-boundary, NaN, handled-error and base-error
scenarios. All **143** unpacker cases run: **142** pass with the one existing
legacy native expected failure. Native/Python version identifiers both advance
to 0.4.20; the native change itself is only the version identifier.

All **274** candidates regenerate in fixed-tool batches from unchanged verified
full-CLI exports. The generation record now also hashes modern_backend.py,
as well as the unchanged Rust source and backend binary. Raw and composite
audits each add **70 matching units across 46 modules**, with **zero previous
matches lost**. Raw recovery reaches **13,422 / 14,291** and **67** complete
modules; composite recovery reaches **13,428 / 14,291** and **68**.
No additional retained body override or isolated-source insertion is used.

Four complete readable modules are published with preserved APIs, docs,
registration/import contracts, SQL values, error handlers and mutation behavior:
astronomy_cache_manager.py (24 units), max_peak_tracker.py (12), the statistics
dev_mode.py (14), and production_operational_reforecast.py (23). This adds
**73 readable whole-module units**. Each of 54 empty artifacts and six
unneeded folded-expression layers is removed only after whole-tree proof.
Descriptive internal names and black==25.1.0 formatting retain the complete
matches. Required shared-return expressions remain where removing them would
change the verified code shape.

Twelve new application methods check the four modules with owned stand-ins:
five cache/peak methods cover 49 paired database/read/threshold/error/cancel
scenarios, plus two original/recovered singleton contexts. They preserve
underlying cache references, loaded flags, partial writes and the original
swallowed daily-load failure behavior. Three DEV-state methods cover 207
paired expiry, identity-vs-truth, published/privacy, callback validation/status,
cancellation and owned HTTP-response scenarios. No real token or service is
used; exception logging is checked to exclude the owned token text.
Four operational methods cover 31 paired future-row, deduplication, shallow
copy, sun-gate, scaling, generation/publish, row-count, listener and lock
scenarios. No inverter, forecast service or live database runs. Logged
exception objects are compared by type/message rather than distinct object
identity; concrete success values accompany paired outcomes. These stand-in
checks do not establish full HA middleware/service behavior.

The full selected application suite passes **126 methods**. A final composite
audit after readable refactoring retains **13,428** matches. Public source
hashes and whole/isolated code-unit scope are checked independently before
commit. Readable whole coverage reaches **68 modules / 1,516 units**; isolated
coverage stays **15 files / 22 distinct units**, plus seven generators.
Remaining **789 instruction** and **74 exception-table** failures leave **863**
unverified units. The full 274-module source/readability objective remains
active and unachieved. Sources remain on the development recovery branch.


## Joint callable-value and shared-return checkpoint

The three statistics database getters in forecast_collector.py,
hourly_aggregator.py and power_sources_collector.py reuse an existing manager,
create one if absent, and reconnect one if disconnected. Reference bytecode
loads the factory attribute as a callable value (LOAD_ATTR flag 0 followed by
PUSH_NULL) and retains one shared coroutine return block. Direct reconstructed
calls use the method optimization, while the compiler duplicates the short
return tail. Repairing either shape alone fails complete-unit comparison.

Unpacker 0.4.21 extends its existing callable-value source pass with a bounded
joint trial. After an attribute-only trial fails, keep that trial while testing
one through four folded true-expression layers around the scope's existing
returns. Reuse the original attribute, receiver, argument and return expressions;
do not create runtime predicates or guess missing work. Collect returns only
within their own scope. Preserve all instruction and exception-dispatch checks,
revert unsuccessful neighbors, and require every previously matching unit to
survive the final comparison. The report's existing
verified_attribute_callable_values field includes the three proved paths.

Two new owned test methods establish that the separate trials both fail and
the joint trial matches. Twenty-one paired coroutine scenarios cover creation,
reuse, reconnection, descriptor lookup, arguments, errors and cancellation.
Nested class scopes and unaffected functions remain intact; changed literals,
receivers and predicates are rejected with their source restored. All 145
unpacker cases run: 144 pass with the one existing legacy native expected
failure. Native/Python version identifiers both advance to 0.4.21; the native
change is only its version identifier. Backend 0.6.13 remains unchanged.
The source-recovery, native-build and security GitHub workflows all pass on
unpacker commit 3667ff1eff4319bb9705f35306dd3a9fdf301e39.

All 274 candidates regenerate in six fixed-tool batches from unchanged verified
full-CLI exports. Raw and composite audits each gain three matching units and
lose none. Raw recovery reaches 13,425 / 14,291 and 67 complete modules;
composite recovery reaches 13,431 / 14,291 and 68. The comparison rules are
unchanged, so all three gains come from reconstructed source. Generation
records retain backend-source, backend-binary and modern_backend.py hashes.

Publish the three getters as isolated, readable methods in class compilation
contexts. Preserve signatures, relative-import names and levels, and master
versus collector hass attributes. Rename private aliases and result locals to
DatabaseConnectionManager, get_manager and manager; full subtree proof after
black==25.1.0 confirms equivalence. Necessary folded callable/return expressions
remain with an explanatory module comment. Containing modules still fail other
units, and the isolated methods receive no retained-body override or additional
corpus credit. No integration or original native runtime is imported.

Three new application methods compare 90 paired scenarios using an owned
relative importer, a factory descriptor and a manager stand-in. Check concrete
returned-manager identity, connection state, import contract, lookup/argument
order, creation versus reuse versus reconnect, import/getter/property/create/
connect failures and coroutine cancellation. These preserve the original
exception boundaries and do not model live database middleware. All 129
selected application methods pass; the newly formatted getter checks also pass
against the new stage's unchanged exports.

Readable whole coverage stays at 68 modules / 1,516 units. Isolated coverage
becomes 18 files / 25 distinct units (20 manifest entries, with one overlapping
pipeline subtree), plus seven separately tallied generators. Metadata now
derives non-retained function counts directly from the manifest: 18 such units
in 16 files. Retained substitutions stay at two definitions / eight subtree
units. This corrects a stale non-retained tally without creating corpus gains.
Remaining 786 instruction and 74 exception-table failures leave 860 unverified
units. The full 274-module source and readability objective remains active and
unachieved; the recovered sources remain on the development branch.


## Decorated scope mapping and complete diagnostic modules

CPython assigns decorated functions the first decorator's source line in
co_firstlineno. The callable-value and shared branch-return source passes
indexed AST definitions by their def line. That silently skipped properties,
static methods and class methods. Use the first decorator's line when present,
otherwise the existing node line; preserve name/line ambiguity checks and all
full-unit proof gates. This corrects which existing trial is eligible. It does
not normalize additional instructions or exception-table differences away.

Unpacker 0.4.22 includes four new owned methods covering property descriptor
order and errors, stacked decorators, duplicate method names in distinct
classes, coroutine cancellation, failed neighboring trials and idempotence.
Their behavior loops have 22 paired scenarios. One full native-export/modern
fixture adds 12 paired property and static-method dictionary-return cases and
requires complete code-tree comparison. All 150 unpacker cases run: 149 pass
with the existing legacy native expected failure. Native/Python identifiers
advance to 0.4.22; native code changes only its identifier. The unchanged
backend remains 0.6.13. GitHub source recovery, native build and security checks
pass on unpacker commit 2f52c0ace8daa132980c8a776432315f65337488.

Corpus probes find 18 branch-return gains and no losses; the corrected callable
lookup has no additional gains on this snapshot. Regenerate all 274 candidates
in six fixed-tool batches from the same verified full-CLI exports. Raw and
composite audits confirm the same 18 gains, zero previously matching units lost
and no comparison-rule change. Raw fidelity reaches 13,443 / 14,291 and 71
complete modules; composite fidelity reaches 13,449 / 14,291 and 72.

Publish four whole readable modules: core_coordinator_init_helpers.py (12
units), sfml_weather_provider.py (55), Weather Fusion's sensor.py (50) and
sensor_diagnostic.py (146). Preserve public APIs, documentation, import and
registration names, class attributes, SQL strings and parameters, error
handlers and mutation behavior. Rename private locals to describe configuration
capacities and panel groups; raw, normalized and grouped weather rows; weighted
temperatures, rain and time zones; calibration and sensor state; AI models,
workflow progress, health timings and scheduling fields. Whole-module proof
after each cleanup and black==25.1.0 formatting retains all matches. Remove
261 empty statements and eight unnecessary folded-expression layers; keep
required compiler-shape edges. This adds 263 readable whole-module units.

A naming pitfall is exposed by the diagnostic module. Giving an inline
comprehension's hour/minute/event and a subsequent loop's bindings identical
names merges originally distinct local slots. Likewise a public-status branch
and a later detailed-model branch have separate bindings even when both refer
to model data. Full-unit proof rejected the naive rename in three methods.
Use scheduled_hour/scheduled_minute/scheduled_event versus the loop's names,
and model_for_status versus model. Recheck the entire module before writing or
publishing it. Readable names must not collapse original bindings or alter
closure ordering merely because their human descriptions overlap.

Thirteen new application methods use owned dependencies. Two configuration
methods cover 25 paired capacity precedence, legacy fallback, defaults, panel
filtering, clipping, option, path and configuration-property scenarios. The
returned configuration constructor and panel normalizer are stand-ins; dataclass
annotation/registration instructions are verified statically. Five weather
methods cover 68 paired UTC/DST/timestamp, row serialization, numeric filtering,
weighted-summary, precipitation, record/month/year, leap-day, snapshot query and
error/timeout/cancellation calls. Own the clock and database responses; verify
query order, the 731-day lower bound, hourly-over-prediction override, empty
availability and concrete output values. Invalid Kepler IDs still raise the
original unhandled conversion error; no new fallback is invented.

Two Weather Fusion methods compare 17 paired sensor contexts for learning
phases, calibration gating, accuracy conversion/rounding, status/icons,
source/blend fields, learning timestamp parsing and base device metadata.
Four main diagnostic methods compare 24 contexts for workflow copy/duration/
redaction, update timestamps and health thresholds, public versus DEV model
fields, candidate age/deadlines, and strict future scheduling/Sunday boundaries.
Provide controlled coordinator, base-sensor, clock and formatting dependencies;
no Home Assistant module, forecast service, native runtime or real database
executes. Whole-module bytecode proof covers the other code units; selected
behavior checks do not claim live HA middleware equivalence. All 142 selected
application methods pass after formatting.

Whole readable coverage reaches 72 modules / 1,779 units. Isolated evidence
stays at 18 files / 25 distinct units, plus seven generators. The isolated gate
formatter is now covered by its complete diagnostic module too; update its
fixture documentation/hash while retaining its tests, and count neither an
override nor an additional corpus gain. The two retained body substitutions
remain unchanged. Independently check all whole/isolated source hashes and
code-tree proofs before commit. Remaining 768 instruction and 74 exception-table
failures leave 842 unverified units. The full 274-module source and readability
objective remains active and unachieved; sources stay on the recovery branch.

## Disjoint investigation and coupled repairs (0.4.24 / 0.6.15)

Five investigation shards partitioned the 842 failing units from the decorated
scope checkpoint by whole-module ownership. Their 169/169/168/168/168 unit
assignments are recorded in `recovery/investigation-842-units.json`; all files
belonged to exactly one shard. The environment permits three children alongside
the main thread, so the five initial investigators ran in two batches. Ongoing
rounds use the maximum available parallel investigators, retaining disjoint
ownership. Investigators inspect immutable tools and private scratch fixtures;
only the main thread authors tracked repairs, tests, builds and commits.

Several defects need coupled trials. Adjacent copied handler fragments can only
be reunited if their existing final return is also restored inside the reunited
try. Structural repair alone and return movement alone both fail. The accepted
trial matches the complete lexical unit and retains every prior match after
unaccepted neighbors are reverted. Genuine independent handlers, intervening
work, else/finally, altered calls and cancellation remain significant. Empty
try/finally return movement is separately proved; a ContextVar writer must see
its origin throughout an await, with reset after success, failure or cancellation.

Other source trials restore existing keyword-unpack boundaries, chained
comprehension comparisons and loop continuation scope. `call(**{**a, **b})` can
silently overwrite a duplicate that `call(**a, **b)` must reject. Restoring the
original DICT_MERGE boundaries therefore changes real behavior; do not flatten
all dictionaries indiscriminately. A comparison chain must evaluate its middle
expression once; two source filters may call it twice. The loop trial restores
an accepting body behind a rejected-continue guard without absorbing nested
loop continuations. Every trial uses the same full instruction and exception
comparator; no new business predicate is inferred.

The backend also retains explicit unnamed-handler returns through NOP,
NOT_TAKEN and CACHE padding following POP_EXCEPT. A dropped `return False` can
turn a failed persistence/validation operation into apparent success. Owned
async fixtures include three padding sizes, both error classes and cancellation.

The first inline-comprehension repair, backend 0.6.14, was insufficient. Its
owned four/six-target fixtures and CI passed, but full-corpus retention found
97 gained units and five lost matches. Blanket suppression of an inner
POP_ITER failed for simple targets whose iterator was still present. The
corrected 0.6.15 model restores the iterator consumed while reconstructing an
unpacked tuple target, so ordinary cleanup operates on the correct stack slot.
Mixed simple/unpacked fixtures preserve exact co_code and exception tables,
ordinary loops afterward, saved variables and errors. This demonstrates why
positive tests and net match counts cannot replace set-wise retention.

The corrected `investigated-repairs-02` run regenerated all 274 candidates from
unchanged full CLI exports with a fixed tool source/binary hash. Raw evidence
matches 13,540 units and 79 whole modules; the composite adds the same six units
from existing proven bodies/readable modules, reaching 13,546 and 80. Both gain
97 and lose zero against `decorated-scopes-01`. There are 745 failures remaining
(672 instructions, 73 exception tables). The normalizer/comparison rules did
not change. No vendor modules or native runtimes were imported and all payload
hashes remain unchanged.

Eight new readable modules contribute 275 nested units, bringing complete
readable recovery to 80 modules/2,054 units. Cleanup removed 67 empty artifacts
and nine unnecessary folded expression layers, each under complete-module proof;
remaining folded edges are needed for the original compiler control flow.
Private locals have descriptive names, while public signatures, original docs,
SQL, units and decisions remain intact. Paired fixtures cover quality thresholds
and model comparisons, grid-charge freshness/force priorities, context isolation,
forecast/error-history conversion failures, circuit-breaker state transitions,
corrected statistics/export, and both offline validators. The validator tests
use owned dependency stand-ins to check call/error routing and signature input,
not to claim cryptographic implementation testing or grant an actual license.

Subsequent exact investigative findings remain queued for main-thread fixes.
Matching only instructions with a different exception span is deliberately
unaccepted, including the current hour-factor and loop-cleanup counterexamples.


## Statement scopes and evaluation identity (0.4.25 / 0.6.16)

The `statement-scopes-01` checkpoint regenerates all 274 modules using frozen
unpacker `3f2845a2a72fb192beefc88c9bad79a1f13d4ebc` and backend
`6f49dd286e9918bc1fd171816ad596c9ec606a13`. Original bytecode exports are unchanged.
Raw source proves **13,709/14,291 units and 103 complete modules**. The composite
with the existing two verified definition overrides proves **13,715 units and
104 complete modules**. Both views gain **169 and lose zero** against
`investigated-repairs-02`; no comparison rules changed. **576 units remain**:
512 instruction mismatches and 64 exception-table mismatches. All candidates
compile, and no integration module or vendor runtime was imported.

The investigators retain disjoint original whole-module assignments. The
harness permits three simultaneous investigators alongside the main thread;
five inventories are rotated through those slots. They provide read-only
probes and owned reproducers. All tool fixes, readable source, tests, builds,
commits, and publication are authored by the main thread. Subsequent probes
remain separate evidence until they pass a combined corpus retention audit.

Source repairs can require several coupled changes. The new bounded trial
runner reuses existing statements and predicates, proves the owner and every
nested code unit, and finally retains every previously matching module unit.
It rolls back failed trials and skips accepted nested scopes whose old AST
objects have been replaced. Async-context suffix trials restore the original
boundary before swallowed work. Protected-tail and trailing-cleanup trials
restore real try/finally scopes, including cancellation and suppressed errors.
Individual return-value and constant-return-statement forms retain distinct
compiler exit edges; neither form is accepted merely because its value looks
right. Legal bare exits also restore async generators without adding returned
values. Original callable access and shared return shapes are proved jointly
where needed. Calls, branches, exception dispatch, and cleanup remain significant.

The backend now distinguishes a COPY of one evaluated expression from two
structurally identical expressions by stack-value identity. Thus independent
`read(), read()` evaluations stay independent, while a real chained assignment
can share its value. It preserves explicit runtime `slice()` calls, including
global shadowing; only actual slice constants become colon syntax, including
multi-axis constants. Walrus reconstruction retains COPY-derived assignment,
truth order and captured cells. Generator declarations are tolerated only for
actual free variables, with unrelated nonlocal/global declarations rejected.
Modern loop-prefix normalization requires contiguous aligned EXTENDED_ARG
bytes rather than guessing across caches or arbitrary gaps.

Ten additional complete modules have readable source and descriptive private
locals. This brings the published readable total to **90 modules / 2,387 units**;
that count is deliberately smaller than the 104 fully matching raw/composite
candidates. Cleanup removes **191 empty artifacts and nine unnecessary fold
layers**, with complete module proof after each change and after formatting.
The manifest's aggregate unit total is recomputed from its entries, correcting
a stale summary field without adding corpus coverage.

Similarly named main/Energy AI modules must not be copied blindly. Strict
whole-tree proof rejected whole-file copies with different annotation imports,
clock access, frost-event rules, and learning exclusions. Each reconstruction
keeps its original AST and public contracts; only audited private-local labels
are reused. A private rename that collided with a public parameter was also
rejected before publication. Closure names preserve the compiler's capture
ordering. The main weather MLP has a different input/validation contract from
its Energy AI counterpart and is reconstructed from its own reference.

Validation passes **167 unpacker cases (166 passes, one existing expected
failure), 35 Rust tests, 43 Python 3.14 backend behavior tests, and 169 selected
application test methods**. Ten new application methods compare context routing,
logging, learning exclusions, frost physics, feature-importance permutations,
callbacks, cancellation, ridge training/holdouts, MLP gradients and day-blocked
quality contracts. Main and Energy AI numerical/frost modules are checked
against their respective references using owned dependencies and fixed clocks.
This selected behavior coverage does not establish full integration equivalence.
The complete goal remains open.

## Residual statement scopes and preserved evaluations (0.4.26 / 0.6.17)

The `residual-scopes-01` checkpoint freezes unpacker
`1eee295fde99642fa747b6dfa1d21e62e2509717` and backend
`7f9cfd7902668601b8ca4ad0d413cd29e16bb11d`. All 274 candidates compile and all
original bytecode exports are unchanged. Raw generation proves **13,801/14,291
units and 115 complete modules**; the composite proves **13,807 units and 116
complete modules**. Each view gains **92 units and loses zero** against
`statement-scopes-01`, using unchanged comparison rules. **484 units remain**:
419 instruction mismatches and 65 exception-table mismatches. Every previously
matched unit and previously complete module is retained. No vendor module is
imported. Generation uses disjoint batches of four worker processes, with at
most two batches active; investigators occupy three harness slots separately.

Several repairs require joint structural trials. Adjacent fragments with
identical handlers can represent one original try, but genuine independent
tries must remain independent. Handler-prefix trials reuse an existing terminal
return only when full proof establishes its original location. Loop handler
continuations are tried only in loop-owned handlers, with bounded combinations.
Shared implicit exits, protected returns, and predicate shape changes are tested
together because either change alone may reproduce instructions while missing
exception coverage. Failed candidates are rolled back, including syntactically
illegal continues. The trials reuse existing expressions, work, and exception
types; they do not invent business calculations. Existing full owner/child and
module-retention checks decide acceptance.

Compiling the candidate AST directly avoids unparsing and reparsing a large
module for every trial. This optimization changes no comparison rules. The
initial source round trip, final emitted source audit, and separate whole-corpus
raw/composite checks remain necessary: a successful intermediate AST compilation
does not prove the published source text.

Four native discoveries have owned reproductions:

- `FOR_ITER` is synchronous even when its collection is an awaited expression.
  Genuine async iteration uses its separate `GET_AITER`/`GET_ANEXT` protocol.
- Long COPY-derived assignments can terminate in `STORE_FAST_STORE_FAST`.
  Missing that store caused false walrus reconstruction, repeated factory calls,
  and different object identities. Three-to-six-target tests preserve one call,
  identity, None values, and raised errors.
- Nested conditional expressions can occupy a comprehension's then arm as well
  as its else arm. Pure-value scanning must retain the complete conditional
  value rather than discard the comprehension body.
- Padding before a protected return requires both meaningful-tail recognition
  and consuming the carried return value inside its original try. Tests include
  NaN/infinity conversion, parsing failures, and errors in later unprotected
  calls. They reject widening the handler to unrelated work.

Two more complete readable modules retain original documentation, APIs and
validation rules: Energy AI's ridge model and central consumption-learning
store. Cleanup removes **28 empty artifacts** with full proof after each edit
and after formatting. The published readable total is **92 modules / 2,453
nested units**, distinct from the 116 complete candidate modules. The Energy AI
ridge model deliberately retains its own older training contract: it fits all
samples before the final await, and cancellation can therefore leave trained
state. Copying the main model's holdout implementation or strict restore API
would fail equivalence. Consumption-store tests cover source-boundary changes,
bounded transient retries, permanent failures, cancellation, and atomic interval
writes without executing integration imports or connecting to Home Assistant.

Validation passes **176 unpacker cases (175 passes, one existing expected
failure), 35 Rust tests, 47 Python 3.14 backend behavior tests, and 171 selected
application test methods**. The two new application methods cover eight paired
scenarios across the model and store. All published whole modules and isolated
units receive separate source-hash and static-proof checks. These checks remain
selected behavioral evidence; full integration equivalence and the complete
source-recovery goal remain open.


## Existing values, bindings and nested comparisons (0.4.27 / 0.6.19)

`substantive-values-01` freezes unpacker
`7a5673ccad4b527172a35af2b3ff8935f0993fd7` and native backend
`a1218b226b5d151e371e54272df74deadc4e886d`. It regenerates every protected
module from the same complete exports, using four workers in one bounded
pool. Both raw and composite comparisons gain **14 units with zero losses**
against `residual-scopes-01`, retaining every earlier complete module. The
composite proves **13,821/14,291 units and 120 complete candidate
modules**; raw generation proves **13,815 units and 119 modules**.
**470 units remain**. All 274 source candidates compile, original inputs
and decrypted PYC exports are unchanged, no vendor module is imported, and the
comparison rules and two existing definition overrides are unchanged.

The source repair trials restore only existing expressions and their ownership:

- Inplace operators retain one evaluation of an indexed or attributed target
  and preserve mutable identity. Genuine binary assignments fail the trial.
- Optional expanded rows retain their singleton tuple boundary; flattening a
  `(label, value)` pair changes the collection's elements.
- Container expressions followed by a bare return can carry the original
  result. A genuine discarded container is retained.
- An unaliased dotted import binds its top package. Restoring that binding
  differs observably from importing the submodule as an alias.
- Conditional values and incorrectly paired generator filters recover their
  compound guards and short-circuit order only with complete subtree proof.
  The containing function may already match while its generator child fails;
  trial eligibility therefore considers all descendants. Nested captures and
  already matching neighbors remain protected by the final module proof.

The native backend also preserves ordered keyword expansions, including named
keywords between `**` arguments. It keeps constructed-dictionary boundaries:
internal DICT_UPDATE overwrites differ from call-level DICT_MERGE duplicate
rejection. Tests retain evaluation order, reject duplicate/nonstring keys and
nonmapping expansions, and ensure an early failure prevents later factories.

Chained comparisons in comprehension value ternaries retain prefix arithmetic,
mapping keys, and other existing element work. The retained copied middle
operand belongs to the chain rather than the element expression. Async-context
constant early returns are recognized by their bounded exit protocol; genuine
calls with three None arguments remain calls. Tests cover errors, suppression,
cancellation and exit failures. These fixes do not claim to solve negated-chain
ternaries or the separate computed-return/open-context investigation.

Three additional readable modules preserve each module's own imports and
documentation: the main and Energy AI weather calculators, and Energy AI's
best-hour calculator. They add **50 readable nested units**, remove **54 empty
decoder artifacts**, and pass complete proof after local renaming and
formatting. No shared-return fold was removed. The readable subset is now
**95 whole modules / 2,503 units**, separate from candidate coverage. Their
three paired test methods cover numeric limits, invalid inputs, weather
fallbacks, local clocks, season conversion, blended factors, database results
and cancellation. All **174 selected application test methods**, **183
unpacker cases (182 passes and one existing expected failure)**, **35 Rust
tests**, and **50 Python 3.14 backend behavior tests** pass.

Three investigators run concurrently under the four-agent harness limit, with
five original non-overlapping inventories rotated through those slots. Their
private whole-owner oracles identify further retry, transaction, handler,
context and loop defects. Those oracles are excluded from this checkpoint's
gains until authored and independently validated in the main thread. All
tracked changes, tests, builds and commits remain root-authored.


## Computed context returns and complete reuse audit (0.4.27 / 0.6.20)

The `open-context-return-01` checkpoint uses native backend
`97c14d709d8379944c4469df8038e12313e2deba`. The source API's code remains at
0.4.27; unpacker commit `582d40d813ab18b32cf6c875b652c48286d1de72` updates
its CI dependency and documentation only. Every native output is compared.
Five changed modules are regenerated; 269 modules reuse source only after
exact raw output and diagnostic equality and an unchanged frozen API hash.
The normalized input hashes and reused source hashes are checked separately.
All 274 modules still receive the complete original-bytecode audit, so source
reuse omits no fidelity or retention checks. Generation takes 233.8 seconds
with four workers, versus the preceding full generation's 934.7 seconds.

Out-of-line loop handlers can extend an open context's region beyond the
normal computed-return tail. The decoder then simulated protocol slots as a
call to None, losing the returned list, scalar or mapping. The existing pure
exit-tail matcher now closes a topmost open With while carrying its existing
value. Conditional arms and already closed With statements retain their
separate handling; genuine user calls remain governed by the protocol bound.
Owned synchronous/asynchronous fixtures make 126 paired comparisons across
normal returns, rejected rows, body failures, entry and exit errors,
suppression and cancellation, with exact instructions and exception tables.

Raw generation now proves **13,818 units / 119 complete candidate modules**;
the composite proves **13,824/14,291 units / 120 complete candidate modules**.
Both gain **three units and lose zero**, preserving every previous complete
module. **467 units remain: 402 instruction and 65 exception mismatches**.
All 274 source candidates compile; 216 are structurally complete, which is
separate from fidelity. Inputs, normalization/comparison rules, the two
definition overrides, and the readable subset of **95 modules / 2,503 units**
remain unchanged. Verification passes 35 Rust tests, 51 backend behavior
methods, and 183 unpacker cases (182 passes and one existing expected failure).
The 174 selected application methods remain supported by their unchanged
source and independently rechecked original-bytecode references.


## Empty comprehension defaults and full retention (0.4.27 / 0.6.21)

The `comprehension-defaults-01` checkpoint distinguishes real inline-comprehension accumulators
from empty default arguments consumed by calls such as `row.get("values", {})`.
For modern compiler setup the accumulator must be followed by `SWAP 2`;
legacy setup retains its separate handling. The mistaken setup previously
created a false nested comprehension level. Nine owned list/set/mapping and
nested-comprehension fixtures verify exact instruction and exception bytes,
plus 45 paired value/error cases and preserved outer locals. Genuine nested,
default-free and empty-tuple controls keep their original behavior.

All 274 native outputs and diagnostics are compared with the prior stage.
218 identical source inputs are reused under the
unchanged frozen 0.4.27 source API; 56 changed
outputs are regenerated. Every resulting module still receives the complete
instruction and exception audit. Generation takes 646.4
seconds with four workers; no proof is skipped by this reuse.

Raw source proves **13819 units / 119 complete
candidate modules**; the composite proves **13825/14,291 units /
120 complete candidate modules**. Both gain
**1 and lose zero**. **466 units remain**.
All inputs and comparison rules remain unchanged, with zero vendor-module
imports. The published readable subset remains **95 modules / 2,503 units**,
verified separately from candidate completeness. Local verification passes
35 Rust tests, 52 backend behavior methods, and 183 unpacker cases (182 passes
and one existing expected failure). The 174 selected application methods
remain supported by their unchanged source and independently rechecked proofs.


## Coupled guards and protected regions (0.4.28 / 0.6.21)

`shared-guards-01` adds bounded, proof-selected rejection chains and existing
guard/context work. Up to four non-overlapping rejection sites can change
together. Negation and inverse equality spellings are distinct trials because
rich-comparison hooks need not agree. A validated inner guard can reclaim an
existing suffix; an empty guard can reclaim an adjacent Try while its success
suffix remains outside. Coupled trials can return an existing result to its
outer protected region. Optional-query handlers can rejoin both their context
and enclosing handler. No new business expression or exception is invented.

Tool snapshots pin API `58f28e804e833a5920a2ff67fb26f22d46f6e2e5` and native
backend `4487dbe65507cc594ca9bc104f52040de86a493a`. An AST comparison proves
that the earlier API algorithms are unchanged and the only new operation is
one final pass before source emission. That pass normalizes its input through
AST unparse/parse, so the prior emitted source supplies exactly the same input
with comments and locations erased. All 274 prior native captures and exports
are hash-checked, then every new source is independently audited. Fresh full
API recovery on three repaired modules and two unchanged controls produces
byte-for-byte identical source and identical report fields. This is reuse of
proved unchanged algorithms, with no omitted fidelity checks. New generation
takes 29.2 seconds with four workers.

Raw and composite views each gain **14 units and lose zero**. Raw source
proves **13,833 units / 122 complete candidate modules**; the composite proves
**13,839/14,291 units / 123 complete candidate modules**. **452 remain:
387 instruction and 65 exception mismatches**. The newly complete candidates
are the STATS common API, EAI DNI tracker and EAI thermodynamics calibrator.
The readable subset remains separately proved at **95 modules / 2,503 units**.
All 274 candidates compile; 216 are structurally complete. Inputs, comparator
rules and the two retained definition overrides remain unchanged, with zero
vendor-module imports. Nine owned methods cover 70 paired guard/date/query
behaviors; all 192 unpacker cases pass with one existing expected failure.
The stable backend retains 35 Rust and 52 behavior methods; the 174 selected
application methods and their unchanged sources remain independently proved.


## Readable DNI tracker without a second coverage count

The EAI DNI tracker already matches in `shared-guards-01`. Its source now names
local rows, hours, history values, bounds, dates and snapshots, removes empty
string/pass artifacts, and retains the original docstrings and attribution.
Black 25.1.0 formats the source. The entire 14-unit module still matches both
instructions and exception dispatch. Constant-folded final None expressions
remain because they retain an original compiler edge; deleting them would
change fidelity. Existing end-of-day zero-placeholder behavior is preserved.

Four paired test methods instantiate only the proved class with owned clocks
and database dependencies. They cover invalid/old dates, history maxima,
self-healing, load idempotence, recording bounds, seven-day rollover, statistics
copy isolation, snapshot keys, save/load failure and cancellation. The full
selected application suite passes **178 methods**. Published readable totals
become **96 modules / 2,517 units**; corpus matches remain **13,839/14,291**,
with **452 unresolved units**. No integration or native PyArmor runtime is
imported, no comparison rule changes, and no Home Assistant operation occurs.


## For-loop OR and nested computed context returns (0.4.28 / 0.6.22)

The `nested-context-loops-01` checkpoint fixes two bounded native failures.
A backedge to an enclosing For header must not be classified as a rotated
While condition when joining OR predicates. Known While/backedge, Continue
and Break/else controls retain their original handling. Eleven owned layouts
cover eight accepting condition/else forms and three strict controls, with
41 paired value/error checks.

A computed value can remain on the stack across two or three context exits.
The decoder now consumes the complete bounded exit sequence, places its
return in the closed inner context, and keeps outer contexts open for their
suppression paths. Closing every outer context at once is incorrect: it emits
stray None protocol calls and loses scope. Owned synchronous/asynchronous
scalar, tuple, mapping and awaited-result cases cover one/two/three contexts,
entry/body/exit failures, suppression and cancellation in 168 paired checks.
Return-after-inner controls retain their distinct outer scope.

Immutable snapshots pin native `ffa3daee309298e1350e0c1dffcae7c6547deb1b`
and source API `58f28e804e833a5920a2ff67fb26f22d46f6e2e5`. Every native
output and diagnostic is compared; 200 identical sources are reused and 74
changed sources regenerated. Generation takes 1,034.6 seconds with four
workers. All 274 modules then receive independent raw and composite fidelity
audits. Raw gains **11** to **13,844 / 125 complete candidates**; composite
gains **10** to **13,849/14,291 / 125 complete candidates**. Both lose **zero**.
One raw gain was already verified independently in the composite, so it is
not counted twice. Newly complete candidates are the main feature engineer
and STATS modern analytics reader.

**442 units remain: 377 instruction and 65 exception mismatches**. All 274
candidates compile; 217 are structurally complete, which is separate from
fidelity. The readable subset remains **96 modules / 2,517 units**, and its
178 application methods retain their separately checked proofs. Comparator
rules, input payloads and retained definition overrides remain unchanged.
Local verification passes 35 Rust tests, 54 native behavior methods and the
192 released unpacker cases (one existing expected failure); no vendor module
or native PyArmor runtime is imported.
