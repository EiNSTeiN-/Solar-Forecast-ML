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

Simple import and from-import markers also have independently tested lowering
rules. Both the protected tuple importer and the ordinary interpreter import
sequence are compared as explicit module/relative-level/name/binding contracts.
Swapping imported aliases fails comparison. Complex attribute and container
marker expressions still require separate rules and remain failures.

## Discovered pitfalls

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
- The latest full audit reports **10,870 matched units out of 14,291** and
  **19 complete module matches**. Those 19 sources (277 code units) are published
  under `recovery/verified_units/`, with original and reconstructed SHA-256
  evidence in `recovery/verified-manifest.json`. All 19 have undergone the
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
- This checkpoint adds 121 matching code units with **zero previously matching
  units lost**, as recorded in `recovery/latest-transition.json`. The full
  source-fidelity and readability goals remain incomplete.
- Fifteen more module sources now use readable variable names and formatting,
  including the newly verified Energy AI feature engineer. Empty-string
  artifacts are removed, original docstrings and literal values are preserved,
  and the SQL schema's escaped text is readable SQL inside multiline literals.
  The selected behavior suite now has **18 test methods**. Added checks cover
  allocation and rounding, radiation geometry, billing and meter overlap,
  consumer configuration and summaries, conservative forecast caps/freshness,
  weather evidence/provenance, feature extraction aliases and sequences, and
  electricity price calculations. The added methods perform **1,248 paired
  function comparisons** in addition to the earlier checks. Tests compare
  outputs, mutations and existing
  exception behavior with the original code. Known default values are attached
  explicitly to selected pure functions without executing their module bodies.
- The unpacker is now **0.4.5-einstein.1** at commit
  `f255e38072b2876387e3fb01ef613002d102ab7e`. Its 75 regression cases report
  74 passes and one documented expected failure in the legacy native backend;
  the optional modern context-manager path passes. The unchanged modern
  backend remains at `6fb29332f605964723a697058f78a504c6868838`.
  Its pinned backend and native build also pass the unpacker fork's
  [GitHub source regressions](https://github.com/EiNSTeiN-/Pyarmor-Static-Unpack-1shot/actions/runs/38182350990)
  and [native build](https://github.com/EiNSTeiN-/Pyarmor-Static-Unpack-1shot/actions/runs/38182351024).
- The current full inventory exports all 274 payloads and validates all 14,291
  nested code objects. Source generation produces compilable candidates for
  all 274 modules. The Energy AI database module's nested `try`/`else`/`finally`
  structure now compiles, but structural warnings and a transaction cleanup
  instruction-order mismatch remain. It has not passed fidelity verification.
  Compilable candidates still require full unit verification.
