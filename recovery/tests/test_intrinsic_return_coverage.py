"""Owned witnesses for the remaining strict coroutine-return coverage gap.

Only bytecode compiled from the literal below is changed for the negative
control. Production reference exports and comparator rules remain unchanged.
"""
import dis
import os
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(os.environ['SFML_UNPACKER_DIR']) / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import _varint


class IntrinsicReturnCoverageTests(unittest.TestCase):
    SOURCE = '''async def work(value, convert):
    """Convert a value while preserving the user error handler."""
    try:
        return convert(value)
    except ValueError:
        return "bad"
'''

    def codes(self):
        module = compile_source(self.SOURCE, 'owned-coroutine')
        stock = next(value for value in module.co_consts if isinstance(value, types.CodeType))
        returned = next(op.offset for op in dis.get_instructions(stock) if op.opname == 'RETURN_VALUE')
        instructions = {op.offset: op for op in dis.get_instructions(stock)}
        regions = []
        removed = 0
        for region in dis.Bytecode(stock).exception_entries:
            if not region.start <= returned < region.end:
                regions.append(region)
                continue
            self.assertEqual(instructions[region.target].opname, 'CALL_INTRINSIC_1')
            self.assertEqual(instructions[region.target].argrepr, 'INTRINSIC_STOPITERATION_ERROR')
            self.assertEqual((region.depth, region.lasti), (0, True))
            if region.start < returned:
                regions.append(region._replace(end=returned))
            if returned + 2 < region.end:
                regions.append(region._replace(start=returned + 2))
            removed += 1
        self.assertEqual(removed, 1)
        table = b''.join(
            _varint(region.start // 2, True)
            + _varint((region.end - region.start) // 2)
            + _varint(region.target // 2)
            + _varint((region.depth << 1) | region.lasti)
            for region in regions)
        return stock, stock.replace(co_exceptiontable=table), returned

    def invoke(self, code, value, *, trace_return=False, returned=None):
        coroutine = types.FunctionType(code, {})(value, int)
        previous = sys.gettrace()

        def trace(frame, event, argument):
            if frame.f_code is code and event == 'return' and frame.f_lasti == returned:
                raise StopIteration('owned return-hook failure')
            return trace

        try:
            if trace_return:
                sys.settrace(trace)
            coroutine.send(None)
        except StopIteration as error:
            return ('stop', error.value)
        except BaseException as error:
            return ('raise', type(error).__name__, str(error),
                    type(error.__cause__).__name__ if error.__cause__ is not None else None)
        finally:
            sys.settrace(previous)
            coroutine.close()

    def test_same_instructions_still_fail_the_strict_exception_proof(self):
        stock, hole, _ = self.codes()
        self.assertEqual(stock.co_code, hole.co_code)
        report = compare(hole, stock)
        self.assertFalse(report['all_units_match'])
        self.assertEqual(report['entries'][0]['reason'], 'exception_table')
        for value in (0, 4, '2', 'bad', None):
            self.assertEqual(self.invoke(stock, value), self.invoke(hole, value))

    def test_a_return_hook_makes_the_coverage_difference_observable(self):
        stock, hole, returned = self.codes()
        self.assertEqual(self.invoke(stock, 4, trace_return=True, returned=returned),
                         ('raise', 'RuntimeError', 'coroutine raised StopIteration', 'StopIteration'))
        self.assertEqual(self.invoke(hole, 4, trace_return=True, returned=returned),
                         ('stop', 'owned return-hook failure'))


if __name__ == '__main__':
    unittest.main()
