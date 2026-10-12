# Investigation of the 842 remaining units

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.

This investigation starts from `decorated-scopes-01`: 13,449 of 14,291 nested
units match, 72 entire modules match, and 842 units remain (768 instruction
mismatches and 74 exception-table mismatches). The immutable tool baseline is
unpacker 0.4.22-einstein.1 and backend 0.6.13-einstein.1.

The user requested five independent investigative shards and reserved all
implementation for the main thread. The execution environment permits three
children alongside the main thread, so five agents ran in two batches. All five
completed. They inspected diagnostics and used private scratch probes; they
made no tracked edits, builds, commits, pushes, or Home Assistant changes.

Assignments partition whole modules, preventing two investigators from editing
or diagnosing different methods in the same file. The public
[`investigation-842-units.json`](../recovery/investigation-842-units.json)
records each assigned unit and its original mismatch reason. It contains no
payloads, decoded instructions, runtime files, or secret metadata.

| Shard | Units | Modules | Instructions | Exception tables |
| --- | ---: | ---: | ---: | ---: |
| 1 | 169 | 38 | 156 | 13 |
| 2 | 169 | 41 | 154 | 15 |
| 3 | 168 | 41 | 151 | 17 |
| 4 | 168 | 41 | 159 | 9 |
| 5 | 168 | 41 | 148 | 20 |
| Total | 842 | 202 | 768 | 74 |

Every unit received diagnostic triage. Exact reconstructions were proved for
selected representatives; this does not imply that every failed unit is solved
or that the remaining application behavior has been verified.

## Findings integrated by the main thread

- **Coupled try reunion and return scope.** Shard 2 established that adjacent
  copied handlers and an existing hoisted tail return sometimes need one atomic
  trial. Either repair alone fails. The generalized implementation proves 66
  additional units against the old complete corpus with zero lost matches.
  Its owned async tests cover fetch/conversion errors, cancellation, distinct
  handlers, intervening work, else/finally, and incorrect call arguments.
- **Empty try/finally return scope.** SMC's context origin was reset before its
  awaited writer. Moving the existing return into the empty protected body
  matches the original. Tests verify that the writer sees the EMS origin until
  success, failure, or cancellation completes and that the outer origin returns.
- **Keyword dictionary merge boundaries.** Multiple `**kwargs` arguments must
  retain `DICT_MERGE` duplicate rejection. Combining them into one literal uses
  `DICT_UPDATE` and can silently overwrite. The main thread added complete-unit
  proof trials using only existing call arguments. Tests distinguish genuine
  literal overwrite, duplicate errors, evaluation order, and unproven neighbors.
- **Chained comprehension comparisons.** Shard 1 established that two emitted
  filters can be a single original comparison chain. Only exact proof allows
  reunion, retaining the middle expression's single evaluation, short circuit,
  rich comparison behavior, and exception coverage.
- **Loop rejection guards.** Shards 3 and 5 identified accepting branches whose
  success tails had escaped the guard. The implementation reuses the existing
  predicate/body in a rejected-continue shape. Nested-loop continuations do not
  qualify and existing correct units remain unchanged.
- **Padded unnamed handler returns.** Shard 4 reproduced the loss of an explicit
  `False` return after an unnamed handler, causing failure to fall through to
  success. The backend now scans NOP/NOT_TAKEN/CACHE padding in the post-handler
  trailer. Owned 1/3/9-NOP async fixtures preserve custom/ordinary failures and
  cancellation.
- **Inline nested comprehension cleanup.** Shard 5 reproduced the failure with
  four or six cleared target names on ordinary CPython 3.14 bytecode. The inner
  VM iterator cleanup was discarding the modeled accumulator. The corrected
  backend restores the iterator slot consumed during unpacked
  target reconstruction, allowing normal VM cleanup to retain the accumulator.
  Owned list/set/dict fixtures retain exact bytecode, dispatch maps, saved target
  names, later ordinary loops, and iteration/unpacking errors.

The source-only combined trial establishes 87 gains with zero losses against
the baseline. This is a probe result, distinct from the published full-tool
checkpoint recorded in `recovery/progress.json` and `latest-transition.json`.
The final full-tool audit also includes the two backend fixes.

## Remaining leads and rejected partial repairs

These findings remain useful for subsequent source/backend work. A suggested
shape is not publishable until the full unit and all prior matches pass.

- **Walrus rejection chains:** shared guards with COPY/STORE_FAST assignments
  need a stack simulation that preserves the assigned value. Three small EAI
  numeric/price accessors have exact reconstructed source. Ordinary assignment
  stores must not be misclassified as named expressions.
- **Single-site shared returns:** STATS EMS `_number` needs one folded rejection
  return, whereas folding every return together fails. Bounded per-site trials
  could recover it without changing its predicate or returned value.
- **Conditional return plus shared-edge folding:** two scalar validators and
  `_hour_number` need both return-expression reconstruction and folded edges;
  `_hour_number` also requires traversal of `Try.orelse`.
- **Empty predicate arms:** an EAI energy sum loses its `rows and all(...)`
  condition. The original predicate expressions are present, but need an AND
  return-shape trial rather than an inferred business condition.
- **Finite-and-bounds expressions:** STATS `_bounded_number` needs the finite
  predicate AND range chain together, plus a preserved compiler return edge.
  A clean-looking ternary alone is two instructions short and is rejected.
- **Nested class aliases:** STATS heating `_entity_classes` declares readable
  classes but refers to stale synthetic bindings. Match class creation to its
  actual local binding. The CSV `_sniff` case additionally needs its omitted
  delimiter fallback; binding repair alone cannot prove it.
- **Missing numeric/loop rejection arms:** `_first_numeric` omits a None guard,
  and a small analytics limit accessor loses its range rejection. Preserve the
  loop stack and original comparison polarity rather than reversing operators
  in ways that change NaN or rich comparisons.
- **Exception-only continuations:** STATS `_pragma_names` loses a second lookup
  reachable only through an exception arm after the normal loop-return block.
  An owned fixture reproduces it. Handler jump destinations must remain live
  even when the normal try path exits through `continue`.
- **Value-preserving generator AND:** EAI precipitation semantics uses boolean
  values merged at `YIELD_VALUE`. The backend emits inverted filters instead,
  changing both values and exception coverage. An owned fixture reproduces it;
  filter jumps to the iterator must remain distinct from value merges at yield.
- **Protected return-only tries:** STATS grid-import `_cell` requires two original
  lookup handlers. Ordinary bytecode succeeds, while the protected layout loses
  the first body and later fallback. Recover its existing fallible lookups and
  handler types, retaining the original order.
- **Coupled hour-factor range rejection:** merging tries, repairing the range,
  and folding a return matches meaningful instructions but still fails the
  exception table. This is deliberately unaccepted.
- **Loop timestamp return:** up to eight folded layers and an empty try/finally
  do not prove `_state_timestamp`. Matching only its instruction stream with
  extra exception machinery is insufficient.

The comparison rules were not weakened. Calls, argument evaluation, meaningful
branches, backward jumps, coroutine cleanup, and real exception dispatch remain
significant. Syntax success or native exit code zero is not evidence of source
equivalence.

## Published complete-corpus checkpoint

`investigated-repairs-02` uses backend 0.6.15 and the unchanged source-repair
implementation in unpacker 0.4.24. It proves 13,546 of 14,291 units, gains 97
against the original 842-unit baseline, and loses none in either raw or
composite comparison. Eight new complete modules bring the readable module
count to 80 (2,054 nested units). There are 745 remaining units: 672 instruction
mismatches and 73 exception-table mismatches. Input payloads are unchanged and
no vendor module/runtime is imported.

The initial `investigated-repairs-01` trial used backend 0.6.14 and gained 97
while losing five previous matches. Its blanket inner iterator-cleanup
suppression broke simple-target comprehensions. That candidate was withheld
from recovered-source publication. The main thread replaced the broad
suppression with iterator-slot restoration for unpacked targets, added mixed
simple/unpacked regression fixtures, and regenerated all 274 modules. The
corrected checkpoint retains the original five matches and the new gains.

The ongoing investigator rounds retain disjoint module ownership and focus on
previously unproved residuals. Exact trial findings are evidence for main-thread
implementation, not additional published coverage. In particular, a correct
instruction stream with an incorrect exception cell remains rejected.


## Frozen statement-scope checkpoint and continued residual investigation

The original five whole-module shards are retained, with at most three
investigators active alongside the main thread. Later investigations use pinned
APIs and binaries; they do not edit tracked code or change Home Assistant.
Their individual proofs and rejected trials are diagnostic evidence until the
main thread implements and validates a combined checkpoint.

`statement-scopes-01` uses unpacker 0.4.25 and backend 0.6.16. Across all 274
modules it gains **169 units with zero losses** in both raw and composite views,
using unchanged comparison rules. Composite fidelity is **13,715/14,291** with
**104 fully matching candidate modules**; raw fidelity is 13,709 with 103.
**576 units remain** (512 instructions, 64 exception tables). The published
readable subset is **90 whole modules / 2,387 units**, after ten newly cleaned
modules; selected application checks pass 169 test methods.

The accepted implementation restores async-context boundaries, protected tails,
finally cleanup, individual expression/statement return edges, legal generator
exits, exact COPY evaluation identity, actual slice constants, walrus captures,
and modern EXTENDED_ARG loop prefixes. Full owner/nested proofs and the corpus
retention gate reject partial fixes even when their instruction lists match but
exception coverage differs. The source-generation inputs and comparison rules
are unchanged; no vendor module was imported.

The subsequent investigator rounds identified further displaced guards and
returns, loop types over awaited collections, missing comprehension bodies,
and larger chained-assignment cases. These remain queued, separate from this
checkpoint's coverage. New residual inventories preserve the original whole-
module ownership rather than distributing individual functions across agents.

The refreshed original-shard residual counts are 116, 120, 101, 117, and 122
respectively (576 total), across 34, 36, 32, 34, and 34 module assignments. These
are residuals in the original ownership inventories, not a redistribution.

## Residual scopes checkpoint

`residual-scopes-01` freezes unpacker 0.4.26 and backend 0.6.17 and regenerates
all 274 modules. Both raw and composite views gain **92 units with zero losses**
against `statement-scopes-01`, retaining every previously complete candidate
module. The composite now proves **13,807/14,291 units and 116 complete candidate
modules**. Raw generation proves 13,801 units and 115 complete modules. The
remaining **484 units** comprise 419 instruction and 65 exception-table
mismatches. Inputs, comparison rules, and the two definition overrides remain
unchanged; no vendor integration module is imported.

The main thread implements bounded predicate, handler-fragment, protected
return, handler continuation, and shared-tail trials with complete owner/child
proof. Native changes retain synchronous iteration of an awaited collection,
one evaluation for long chained assignments ending in a fused store, nested
conditional comprehension bodies, and padded return values inside their
original handlers. Owned negative cases reject changed exception scope,
duplicated calls, independent tries merged incorrectly, and lost continuation
semantics. Investigator-only oracle results remain separate from corpus gains.

Two further readable modules preserve Energy AI's own ridge-training and
durable consumption-store contracts. The published readable subset now has
**92 complete modules / 2,453 nested units**, after removing 28 empty decoder
artifacts and checking complete equivalence after formatting. Validation passes
176 unpacker cases (175 passes and one existing expected failure), 35 Rust
tests, 47 backend behavior tests, and 171 selected application test methods.
Three simultaneous investigators continue with the same original whole-module
ownership; all authored changes, builds and commits remain in the main thread.
The refreshed residual inventories contain 100, 105, 84, 94, and 101 units
across 31, 34, 32, 31, and 30 modules respectively. Their union is exactly 484
units with no overlapping assignments; completed oracles are excluded from
future coverage claims unless the current full-stage proof still fails.
