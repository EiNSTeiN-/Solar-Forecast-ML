# Remaining WeatherExpert terminal layout

The last instruction-layout mismatch is
`extra_features/weather_fusion_ai/weather_experts.py`,
`WeatherExpert._get_json`. Its closest source has262 normalized instructions;
the reference has263. The strict comparator still rejects this source, and it
receives **zero recovery credit**. The reference and comparison rules are
unchanged.

The reference has adjacent `RERAISE 0` terminals at normalized indices254 and255.
The conditional false path enters the second copy; the completed cleanup enters
the first. Both copies have identical exception dispatch, including depth and
`lasti`. The closest source shares one terminal between those paths.

A root-authored research comparison redirects only incoming edges to the second
copy onto the first, removes that duplicate, and relocates the remaining targets.
Under that explicit transformation, **all263-reference/262-candidate instruction
graph operations, signatures, unresolved-marker checks and exception dispatch
agree**. No call, predicate, operand, value or handler is discarded. The research
result is [terminal-rethrow-layout.json](../recovery/terminal-rethrow-layout.json).
It is deliberately separate from the production comparator and success counts.

The owned regression
[test_terminal_rethrow_layout.py](../recovery/tests/test_terminal_rethrow_layout.py)
compiles two short literal Python functions: a straight finalizer and an equivalent
one-pass finalizer. Stock CPython3.14.2 emits duplicated versus shared terminal
rethrows. Three tests pass: strict rejection remains intact; the research views
agree; and12 ordinary truth/error route pairs agree. Extra work, a changed
`RERAISE` operand, or different dispatch on one copy fails the research comparison.
Only the owned literal functions execute, with no integration or armor runtime.

This is a concrete compiler-layout alternative, distinct from the seventeen
coroutine-return cleanup gaps documented in
[COROUTINE_RETURN_COVERAGE.md](COROUTINE_RETURN_COVERAGE.md). It does not prove that
an exact ordinary-source spelling is impossible or certify whole-application
runtime equivalence. Accepting terminal sharing as an allowed bytecode variance
would require an explicit equivalence-policy decision; until then this owner
remains strictly unmatched.
