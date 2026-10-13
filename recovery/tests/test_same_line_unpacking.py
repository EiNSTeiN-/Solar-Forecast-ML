"""Owned witness for repeated-target unpacking's verified source layout.

Only literal fixture code is compiled. No protected module, HA service or
external dependency is loaded, and no comparator rule or bytecode is changed.
"""
import asyncio
import os
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(os.environ["SFML_UNPACKER_DIR"]) / "oneshot"))
from bytecode_fidelity import compare, compile_source


SAME_LINE = """async def route(fetch):
    first, second, discarded_component, discarded_component = await fetch()
    return first, second
"""
SPLIT_LINES = """async def route(fetch):
    (
        first,
        second,
        discarded_component,
        discarded_component,
    ) = await fetch()
    return first, second
"""
KEPT_LINE = """async def route(fetch):
    first, second, discarded_component, discarded_component = await fetch()  # fmt: skip
    return first, second
"""
GENERATOR_SAME_LINE = """def route(rows):
    return all(value is None or amount is not None for (value, discarded, discarded, discarded), amount in rows)
"""
GENERATOR_SPLIT_LINES = """def route(rows):
    return all(
        value is None or amount is not None
        for (
            value,
            discarded,
            discarded,
            discarded,
        ), amount in rows
    )
"""
GENERATOR_KEPT_LINE = """def route(rows):
    return all(
        value is None or amount is not None
        for (value, discarded, discarded, discarded), amount in rows  # fmt: skip
    )
"""


def function(source):
    module = compile_source(source, "owned-unpacking")
    code = next(value for value in module.co_consts if isinstance(value, types.CodeType))
    return types.FunctionType(code, {}), module


def invoke(route, values, failure=None):
    calls = []

    async def fetch():
        calls.append("fetch")
        if failure is not None:
            raise failure("owned fetch failure")
        return values

    try:
        return ("value", asyncio.run(route(fetch))), calls
    except BaseException as error:
        return (type(error).__name__, str(error)), calls


class SameLineUnpackingTests(unittest.TestCase):
    def test_split_tuple_returns_agree_but_strict_instructions_reject_it(self):
        original, reference = function(SAME_LINE)
        split, candidate = function(SPLIT_LINES)
        report = compare(reference, candidate)
        self.assertFalse(report["all_units_match"])
        failures = [entry for entry in report["entries"] if not entry["matched"]]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["reason"], "instructions")
        self.assertIn(("POP_TOP", None), failures[0]["reference"])
        for values in ((1, 2, 3, 4), (0, None, [], {}), (), (1,), (1, 2, 3), tuple(range(5))):
            self.assertEqual(invoke(original, values), invoke(split, values))
        for failure in (ValueError, RuntimeError, asyncio.CancelledError):
            self.assertEqual(invoke(original, (), failure), invoke(split, (), failure))

    def test_kept_line_preserves_all_instructions_and_exception_regions(self):
        original, reference = function(SAME_LINE)
        kept, candidate = function(KEPT_LINE)
        report = compare(reference, candidate)
        self.assertTrue(report["all_units_match"])
        self.assertEqual(report["matched_units"], report["reference_units"])
        self.assertEqual(invoke(original, (1, 2, 3, 4)), invoke(kept, (1, 2, 3, 4)))
        wrong = KEPT_LINE.replace("return first, second", "return second, first")
        _, incorrect = function(wrong)
        self.assertFalse(compare(reference, incorrect)["all_units_match"])

    def test_split_nested_generator_target_changes_real_unpacking_instructions(self):
        original, reference = function(GENERATOR_SAME_LINE)
        split, candidate = function(GENERATOR_SPLIT_LINES)
        report = compare(reference, candidate)
        self.assertFalse(report['all_units_match'])
        failures = [entry for entry in report['entries'] if not entry['matched']]
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0]['unit'].endswith('/<genexpr>#1'))
        self.assertEqual(failures[0]['reason'], 'instructions')
        self.assertIn(('POP_TOP', None), failures[0]['reference'])
        scenarios = [[], [((None, 1, 2, 3), None)], [((5, 1, 2, 3), 0)],
                     [((5, 1, 2, 3), None)], [((None, 1, 2, 3), 2), ((5, 1, 2, 3), None)],
                     [((), None)], [((1, 2), 3)], [((1, 2, 3, 4, 5), 6)]]

        def outcome(route, values):
            try:
                return 'value', route(values)
            except (TypeError, ValueError) as error:
                return type(error).__name__, str(error)

        for values in scenarios:
            self.assertEqual(outcome(original, values), outcome(split, values))

    def test_generator_header_alone_can_keep_the_proved_target_layout(self):
        original, reference = function(GENERATOR_SAME_LINE)
        kept, candidate = function(GENERATOR_KEPT_LINE)
        report = compare(reference, candidate)
        self.assertTrue(report['all_units_match'])
        self.assertEqual(report['matched_units'], 3)
        for values in ([], [((None, 1, 2, 3), None)], [((5, 1, 2, 3), 0)], [((5, 1, 2, 3), None)]):
            self.assertEqual(original(values), kept(values))
        wrong = GENERATOR_KEPT_LINE.replace('amount is not None', 'amount is None')
        _, incorrect = function(wrong)
        self.assertFalse(compare(reference, incorrect)['all_units_match'])


if __name__ == "__main__":
    unittest.main()
