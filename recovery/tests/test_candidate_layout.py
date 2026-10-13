"""Owned regression for verified line layout after candidate body assembly."""
import ast
import copy
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(os.environ['SFML_UNPACKER_DIR']) / 'oneshot'))
from audit_units import render_retained_candidate
from bytecode_fidelity import compare, compile_source
from modern_backend import preserve_multiline_return_shapes


class CandidateLayoutTests(unittest.TestCase):
    SOURCE = '''def work(rows, convert):
    """Choose the first convertible row."""
    for row in rows:
        try:
            return (
                convert(row) if row else None
            )
        except (TypeError, ValueError):
            return -1
def neighbor(value, *, step=1):
    """Preserve the keyword interface."""
    return value + step
'''

    def assemble(self, body):
        tree = ast.parse(self.SOURCE.replace('return value + step', 'return value + 20'))
        donor = ast.parse('def neighbor(other, *, step=99):\n' + body).body[0]
        tree.body[1].body = [copy.deepcopy(tree.body[1].body[0])] + donor.body
        reference = compile_source(self.SOURCE, 'owned-original')
        source, selected = render_retained_candidate(
            tree, reference, None, preserve_multiline_return_shapes)
        return reference, source, selected

    def test_body_assembly_preserves_layout_signature_and_execution(self):
        reference, source, selected = self.assemble('    return value + step\n')
        self.assertEqual(selected, ['<module>/work#1'])
        self.assertTrue(compare(reference, compile_source(source, 'owned-assembled'))['all_units_match'])
        original, repaired = {}, {}
        exec(reference, original)
        exec(compile_source(source, 'owned-assembled'), repaired)
        for rows in ([], [0], [2], ['bad'], [1, 2]):
            for converter in (float, int):
                self.assertEqual(original['work'](rows, converter), repaired['work'](rows, converter))
        for value in (0, 2, -4):
            for step in (1, 3, -2):
                self.assertEqual(original['neighbor'](value, step=step),
                                 repaired['neighbor'](value, step=step))
        self.assertEqual(repaired['neighbor'].__kwdefaults__, {'step': 1})
        self.assertEqual(repaired['neighbor'].__doc__, original['neighbor'].__doc__)

    def test_layout_recovery_does_not_certify_a_wrong_retained_body(self):
        reference, source, selected = self.assemble('    return value + step + 1\n')
        report = compare(reference, compile_source(source, 'owned-negative'))
        self.assertEqual(selected, ['<module>/work#1'])
        self.assertFalse(report['all_units_match'])
        self.assertFalse(next(entry for entry in report['entries']
                              if entry['unit'] == '<module>/neighbor#1')['matched'])
        self.assertIn('return value + step + 1', source)


if __name__ == '__main__':
    unittest.main()
