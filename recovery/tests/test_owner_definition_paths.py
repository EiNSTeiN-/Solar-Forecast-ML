"""Resolve isolated definitions without crossing unrelated code scopes."""

import ast
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "recovery_audit_units", Path(__file__).resolve().parents[1] / "audit_units.py"
)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class OwnerDefinitionPaths(unittest.TestCase):
    def test_nested_try_else_and_finally_definitions_keep_lexical_owner(self):
        tree = ast.parse(
            """class Owner:
    async def outer(self):
        try:
            async def inner():
                return self.first
        except ValueError:
            async def inner():
                return self.second
        else:
            async def inner():
                return self.third
        finally:
            async def inner():
                return self.fourth
"""
        )
        for occurrence, attribute in enumerate(
            ("first", "second", "third", "fourth"), 1
        ):
            _, _, owner = AUDIT.named_source_unit(
                tree, f"<module>/Owner#1/outer#1/inner#{occurrence}"
            )
            self.assertIsInstance(owner, ast.AsyncFunctionDef)
            self.assertEqual(owner.body[0].value.attr, attribute)

    def test_sibling_occurrences_do_not_include_other_function_children(self):
        tree = ast.parse(
            """def outer():
    def inner():
        def inner():
            return 'grandchild'
        return inner
    if ready:
        def inner():
            return 'sibling'
    return inner
def other():
    def inner():
        return 'other'
"""
        )
        _, _, sibling = AUDIT.named_source_unit(tree, "<module>/outer#1/inner#2")
        _, _, grandchild = AUDIT.named_source_unit(
            tree, "<module>/outer#1/inner#1/inner#1"
        )
        self.assertEqual(sibling.body[0].value.value, "sibling")
        self.assertEqual(grandchild.body[0].value.value, "grandchild")
        with self.assertRaises(IndexError):
            AUDIT.named_source_unit(tree, "<module>/outer#1/inner#3")

    def test_module_branch_definitions_and_invalid_targets(self):
        tree = ast.parse(
            """if selected:
    def select():
        return 'first'
else:
    def select():
        return 'second'
class Owner:
    pass
"""
        )
        _, _, owner = AUDIT.named_source_unit(tree, "<module>/select#2")
        self.assertEqual(owner.body[0].value.value, "second")
        for path in (
            "<module>",
            "wrong/select#1",
            "<module>/select#0",
            "<module>/Owner#1",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                AUDIT.named_source_unit(tree, path)


if __name__ == "__main__":
    unittest.main()
