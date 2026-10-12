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
- Current work: add full-scope inventory, correct exception cleanup, closure
  captures and annotation reconstruction, introduce static unit comparison,
  and rebuild the source candidates. Exact coverage is recorded in the audit
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
- The latest full audit reports **13,183 matched units out of 14,291** and
  **54 complete module matches**. Fifty-one readable sources (1,074 code units) are published
  under `recovery/verified_units/`, with original and reconstructed SHA-256
  evidence in `recovery/verified-manifest.json`. All 51 published sources have undergone the
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
- The preceding frame/handler checkpoint added 1,059 matching code units with
  zero previously matching units lost. The latest annotation/exit checkpoint
  added **233** matching code units while retaining the earlier verified API
  method. The following guard checkpoint adds 60 further matches without any
  retention, as recorded in `recovery/latest-transition.json`. The full
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
