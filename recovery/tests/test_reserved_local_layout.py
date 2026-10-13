"""Owned witness for retaining an unused compiler slot with a readable name.

Only literal fixture code runs. No integration module or PyArmor runtime is
loaded, and public signatures, real instructions and exception regions stay
significant under internal local-name variance.
"""
import os
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(os.environ['SFML_UNPACKER_DIR']) / 'oneshot'))
from bytecode_fidelity import compare, compile_source


def source(reservation='__assert_armored__'):
    assignments = ''.join(f'    value_{index} = {index}\n' for index in range(61))
    return (f'def select(flag, convert):\n    if False:\n        {reservation} = None\n'
            + assignments
            + '    try:\n        if flag:\n            edge = convert(3)\n'
              '        else:\n            edge = convert(4)\n'
              '        return edge\n    except UnboundLocalError:\n        return "missing"\n')


def function(text):
    module = compile_source(text, 'owned-local-layout')
    code = next(value for value in module.co_consts if isinstance(value, types.CodeType))
    return code, types.FunctionType(code, {})


class ReservedLocalLayoutTests(unittest.TestCase):
    def test_readable_reservation_preserves_instruction_and_exception_bytes(self):
        original, left = function(source())
        candidate, right = function(source('unused_layout_slot'))
        self.assertEqual(original.co_varnames.index('edge'), 64)
        self.assertEqual(original.co_code, candidate.co_code)
        self.assertEqual(original.co_exceptiontable, candidate.co_exceptiontable)
        self.assertTrue(compare(original, candidate)['all_units_match'])
        for flag in (False, True, 0, 1, None):
            self.assertEqual(left(flag, int), right(flag, int))

    def test_deletion_live_access_and_changed_work_are_rejected(self):
        original, _ = function(source())
        removed = source().replace('    if False:\n        __assert_armored__ = None\n', '')
        deleted, _ = function(removed)
        self.assertEqual(deleted.co_varnames.index('edge'), 63)
        self.assertNotEqual(original.co_code, deleted.co_code)
        neutral = source('unused_layout_slot')
        candidates = [removed,
                      neutral.replace('    try:\n', '    unused_layout_slot = 7\n    try:\n'),
                      neutral.replace('convert(3)', 'convert(4)'),
                      neutral.replace('except UnboundLocalError', 'except ValueError')]
        for candidate in candidates:
            code, _ = function(candidate)
            self.assertFalse(compare(original, code)['all_units_match'])


if __name__ == '__main__':
    unittest.main()
