"""Owned controls distinguish a return alias from an actual cleanup hole.

The fixture changes only freshly compiled literal code. It does not execute
protected definitions or relax the production fidelity comparator.
"""

import dis
import os
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(os.environ["SFML_UNPACKER_DIR"]) / "oneshot"))
from bytecode_fidelity import canonical, compare, compile_source
from modern_backend import _varint


class CoroutineReturnAliasProvenanceTests(unittest.TestCase):
    SOURCE = """async def work(flag):
    try:
        if flag:
            return 7
        return 9
    except Exception:
        return 11
"""

    @staticmethod
    def exception_table(regions):
        return b"".join(
            _varint(region.start // 2, True)
            + _varint((region.end - region.start) // 2)
            + _varint(region.target // 2)
            + _varint((region.depth << 1) | region.lasti)
            for region in sorted(regions, key=lambda row: (row.start, row.end, row.target))
        )

    @staticmethod
    def remove_slot(regions, offset):
        """Remove just one two-byte opcode's route, retaining neighboring routes."""
        remaining = []
        for region in regions:
            if not region.start <= offset < region.end:
                remaining.append(region)
                continue
            if region.start < offset:
                remaining.append(region._replace(end=offset))
            if offset + 2 < region.end:
                remaining.append(region._replace(start=offset + 2))
        return remaining

    def codes(self):
        module = compile_source(self.SOURCE, "owned-return-alias")
        stock = next(value for value in module.co_consts if isinstance(value, types.CodeType))
        instructions = list(dis.get_instructions(stock))
        returns = [op.offset for op in instructions if op.opname == "RETURN_VALUE"]
        jump, target = returns[:2]
        argument = (target - jump - 2) // 2
        self.assertEqual(target - jump - 2, 2 * argument)
        self.assertTrue(0 <= argument <= 255)
        intrinsic = next(op.offset for op in instructions
                         if op.opname == "CALL_INTRINSIC_1"
                         and op.argrepr == "INTRINSIC_STOPITERATION_ERROR")
        regions = list(dis.Bytecode(stock).exception_entries)
        user_handler = next(region for region in regions
                            if region.depth == 0 and not region.lasti)
        self.assertTrue(any(region.start <= target < region.end
                            and region.target == intrinsic and region.lasti
                            for region in regions))
        alias_regions = self.remove_slot(regions, jump)
        alias_regions.append(user_handler._replace(start=jump, end=jump + 2))
        operations = bytearray(stock.co_code)
        operations[jump:jump + 2] = bytes((dis.opmap["JUMP_FORWARD"], argument))
        alias = stock.replace(co_code=bytes(operations),
                              co_exceptiontable=self.exception_table(alias_regions))
        hole = alias.replace(co_exceptiontable=self.exception_table(
            self.remove_slot(alias_regions, target)))
        return stock, alias, hole, jump, target, intrinsic

    def invoke(self, code, flag, *, inject_offset=None, inject_event="opcode",
               exception_type=StopIteration):
        # Fresh code avoids tracing-instrumentation caches between test cases.
        code = code.replace()
        coroutine = types.FunctionType(code, {})(flag)
        previous = sys.gettrace()

        def trace(frame, event, argument):
            if frame.f_code is code:
                frame.f_trace_opcodes = True
                frame.f_trace_lines = False
                if event == inject_event and frame.f_lasti == inject_offset:
                    raise exception_type("owned trace witness")
            return trace

        try:
            if inject_offset is not None:
                coroutine.cr_frame.f_trace = trace
                coroutine.cr_frame.f_trace_opcodes = True
                coroutine.cr_frame.f_trace_lines = False
                sys.settrace(trace)
            coroutine.send(None)
        except StopIteration as error:
            return ("stop", error.value)
        except BaseException as error:
            return ("raise", type(error).__name__, str(error),
                    type(error.__cause__).__name__ if error.__cause__ is not None else None)
        finally:
            sys.settrace(previous)
            coroutine.close()

    def test_forward_jump_projection_is_not_an_actual_return_cleanup_hole(self):
        stock, alias, _, jump, target, intrinsic = self.codes()
        operations = {op.offset: op for op in dis.get_instructions(alias)}
        self.assertEqual(operations[jump].opname, "JUMP_FORWARD")
        self.assertEqual(operations[jump].argval, target)
        self.assertEqual(operations[target].opname, "RETURN_VALUE")
        regions = list(dis.Bytecode(alias).exception_entries)
        self.assertTrue(any(region.start <= jump < region.end
                            and not region.lasti and region.depth == 0 for region in regions))
        self.assertTrue(any(region.start <= target < region.end
                            and region.target == intrinsic and region.lasti for region in regions))
        self.assertEqual(canonical(stock).instructions, canonical(alias).instructions)
        self.assertNotEqual(canonical(stock).exceptions, canonical(alias).exceptions)
        proof = compare(alias, stock)
        self.assertFalse(proof["all_units_match"])
        self.assertEqual(proof["entries"][0]["reason"], "exception_table")
        for flag, expected in ((False, 9), (True, 7)):
            self.assertEqual(self.invoke(stock, flag), ("stop", expected))
            self.assertEqual(self.invoke(alias, flag), ("stop", expected))

    def test_alias_opcode_and_actual_exit_follow_distinct_exception_routes(self):
        _, alias, hole, jump, target, _ = self.codes()
        for exception_type in (StopIteration, LookupError):
            self.assertEqual(self.invoke(alias, True, inject_offset=jump,
                                         exception_type=exception_type), ("stop", 11))
        wrapped = ("raise", "RuntimeError", "coroutine raised StopIteration", "StopIteration")
        for event in ("opcode", "return"):
            self.assertEqual(self.invoke(alias, True, inject_offset=target,
                                         inject_event=event), wrapped)
        self.assertEqual(alias.co_code, hole.co_code)
        self.assertNotEqual(alias.co_exceptiontable, hole.co_exceptiontable)
        self.assertEqual(self.invoke(hole, True, inject_offset=target,
                                     inject_event="return"), ("stop", "owned trace witness"))


if __name__ == "__main__":
    unittest.main()
