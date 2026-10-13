"""Owned research controls; the production comparator remains strict."""
from dataclasses import replace
import os
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(os.environ["SFML_UNPACKER_DIR"]) / "oneshot"))
from bytecode_fidelity import canonical, compare, compile_source


def terminal_view(shape):
    """Research only: share adjacent RERAISE0 tails with identical dispatch."""
    def dispatch(index):
        handlers = [(target, depth, lasti)
                    for start, end, target, depth, lasti in shape.exceptions
                    if start <= index < end]
        if len(handlers) > 1:
            raise ValueError("Overlapping handlers need a separate proof")
        return handlers[0] if handlers else None

    removed = {i for i in range(1, len(shape.instructions))
               if shape.instructions[i] == shape.instructions[i - 1] == ("RERAISE", 0)
               and dispatch(i) == dispatch(i - 1)}

    def target(index):
        while index in removed:
            index -= 1
        return index - sum(value < index for value in removed)

    instructions, handlers = [], []
    for i, (name, operand) in enumerate(shape.instructions):
        if i in removed:
            continue
        if isinstance(operand, tuple) and operand[:1] == ("target",):
            operand = ("target", target(operand[1]))
        instructions.append((name, operand))
        handler = dispatch(i)
        handlers.append((target(handler[0]), *handler[1:]) if handler else None)
    return shape.signature, tuple(instructions), tuple(handlers), shape.unresolved


class TerminalRethrowLayoutTests(unittest.TestCase):
    SPLIT = """def work(flag, error):
    try:
        raise error
    finally:
        if flag:
            pass
"""
    SHARED = """def work(flag, error):
    try:
        raise error
    finally:
        while True:
            if flag:
                pass
            break
"""

    def code(self, source):
        return next(value for value in compile_source(source, "owned-rethrow").co_consts
                    if isinstance(value, types.CodeType))

    def test_strict_rejection_and_research_graph_equality(self):
        split, shared = self.code(self.SPLIT), self.code(self.SHARED)
        strict = compare(split, shared)
        self.assertFalse(strict["all_units_match"])
        self.assertEqual(strict["entries"][0]["reason"], "instructions")
        self.assertEqual(terminal_view(canonical(split)), terminal_view(canonical(shared)))

    def test_work_operand_and_dispatch_changes_are_not_hidden(self):
        split, shared = canonical(self.code(self.SPLIT)), canonical(self.code(self.SHARED))
        extra_work = canonical(self.code(self.SPLIT.replace("            pass", "            flag")))
        self.assertNotEqual(terminal_view(extra_work), terminal_view(shared))
        duplicate = next(i for i in range(1, len(split.instructions))
                         if split.instructions[i] == split.instructions[i - 1] == ("RERAISE", 0))
        changed = list(split.instructions)
        changed[duplicate] = ("RERAISE", 1)
        self.assertNotEqual(terminal_view(replace(split, instructions=tuple(changed))),
                            terminal_view(shared))
        regions = []
        for start, end, target, depth, lasti in split.exceptions:
            if start <= duplicate < end:
                if start < duplicate:
                    regions.append((start, duplicate, target, depth, lasti))
                regions.append((duplicate, duplicate + 1, target, depth + 1, lasti))
                if duplicate + 1 < end:
                    regions.append((duplicate + 1, end, target, depth, lasti))
            else:
                regions.append((start, end, target, depth, lasti))
        self.assertNotEqual(terminal_view(replace(split, exceptions=tuple(regions))),
                            terminal_view(shared))

    def test_owned_truth_and_exception_routes_agree(self):
        split = types.FunctionType(self.code(self.SPLIT), {})
        shared = types.FunctionType(self.code(self.SHARED), {})

        def invoke(function, truth, failure_type):
            events = []

            class Flag:
                def __bool__(self):
                    events.append("truth")
                    if isinstance(truth, type) and issubclass(truth, BaseException):
                        raise truth("owned truth failure")
                    return truth

            try:
                function(Flag(), failure_type("owned original error"))
            except BaseException as error:
                return (events, type(error).__name__, str(error),
                        type(error.__context__).__name__ if error.__context__ else None)
            self.fail("Both owned functions must raise")

        for truth in (False, True, ValueError, StopIteration):
            for failure in (ValueError, RuntimeError, KeyboardInterrupt):
                with self.subTest(truth=truth, failure=failure):
                    self.assertEqual(invoke(split, truth, failure), invoke(shared, truth, failure))


if __name__ == "__main__":
    unittest.main()
