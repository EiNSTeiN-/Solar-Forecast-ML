"""Compare isolated filter accessors with their original normalized code.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
Only selected, statically proved functions execute with owned inputs.
"""

import unittest

from pure_reference import isolated_function


def outcome(function, *arguments):
    try:
        return ("value", function(*arguments))
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        return (type(error).__name__, str(error))


class InlineFilterAccessorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.headers = isolated_function(
            "extra_features/sfml_stats/api/gpm.py",
            "extra_features/sfml_stats/api/csv_headers.py",
            "<module>/_csv_headers#1",
            {},
        )
        cls.hours = isolated_function(
            "extra_features/sfml_stats/core/ems.py",
            "extra_features/sfml_stats/core/ems_hours.py",
            "<module>/EMSManager#1/_hour_points_from#1",
            {},
        )

    def test_csv_headers_keep_only_nonempty_strings_and_preserve_duplicates(self):
        cases = (
            None,
            0,
            False,
            {},
            (),
            [],
            "",
            [""],
            ["a", " b "],
            ["a", None, 2, " "],
            ["repeat", "repeat"],
            [b"bytes", "a"],
        )
        original, recovered = self.headers
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(outcome(recovered, value), outcome(original, value))
        self.assertEqual(
            recovered([" first ", None, "", "first", 8]), ["first", "first"]
        )

    def test_csv_strip_calls_truth_checks_and_errors_remain_in_order(self):
        cases = (
            ("truthy", "final"),
            ("false",),
            ("truth_error",),
            (ValueError("first strip failed"),),
            ("truthy", ValueError("second strip failed")),
            ("truthy", "final", "unused"),
        )

        def exercise(function, results):
            calls = []

            class Truth:
                def __init__(self, tag):
                    self.tag = tag

                def __bool__(self):
                    calls.append(("bool", self.tag))
                    if self.tag == "truth_error":
                        raise ValueError("truth failed")
                    return self.tag != "false"

            class Header(str):
                def strip(self):
                    calls.append("strip")
                    result = pending.pop(0)
                    if isinstance(result, Exception):
                        raise result
                    return Truth(result) if result != "final" else "final header"

            pending = list(results)
            return outcome(function, [Header("header")]), calls

        original, recovered = self.headers
        for values in cases:
            with self.subTest(values=values):
                self.assertEqual(
                    exercise(recovered, values), exercise(original, values)
                )
        self.assertEqual(
            exercise(recovered, ("truthy", "final")),
            (("value", ["final header"]), ["strip", ("bool", "truthy"), "strip"]),
        )

    def test_hour_points_keep_integer_hour_gate_and_start_boundary(self):
        cases = (
            [],
            [{}],
            [{"hour": True}, {"hour": 1.0}, {"hour": None}, {"hour": 3}],
            [{"hour": 0}, {"hour": 1}, {"hour": 2}],
            None,
            ["invalid row"],
        )
        original, recovered = self.hours
        for rows in cases:
            for start_hour in (-1, 0, 1, 2):
                with self.subTest(rows=rows, start_hour=start_hour):
                    self.assertEqual(
                        outcome(recovered, None, rows, start_hour),
                        outcome(original, None, rows, start_hour),
                    )
        self.assertEqual(
            recovered(None, [{"hour": True}, {"hour": 1.0}, {"hour": 3}], 0),
            [{"hour": True}, {"hour": 3}],
        )

    def test_hour_getitem_comparison_and_error_paths_remain_separate(self):
        cases = (
            (2, 2, False),
            (None, 2, False),
            (1.0, 2, False),
            (ValueError("get failed"), 2, False),
            (2, KeyError("item failed"), False),
            (2, 2, True),
            (True, False, False),
            (1, 3, False),
        )

        def exercise(function, values):
            calls = []
            getter, item, fail_comparison = values

            class Hour(int):
                def __ge__(self, other):
                    calls.append(("ge", int(self), other))
                    if fail_comparison:
                        raise ValueError("comparison failed")
                    return super().__ge__(other)

                def __lt__(self, other):
                    raise AssertionError("The original comparison must not be inverted")

            class Row:
                tag = "row"

                def get(self, key):
                    calls.append(("get", key))
                    if isinstance(getter, Exception):
                        raise getter
                    return getter

                def __getitem__(self, key):
                    calls.append(("item", key))
                    if isinstance(item, Exception):
                        raise item
                    return Hour(item)

            result = outcome(function, None, [Row()], 2)
            if result[0] == "value":
                result = ("value", [row.tag for row in result[1]])
            return result, calls

        original, recovered = self.hours
        for values in cases:
            with self.subTest(values=values):
                self.assertEqual(
                    exercise(recovered, values), exercise(original, values)
                )
        self.assertEqual(
            exercise(recovered, (1, 3, False)),
            (("value", ["row"]), [("get", "hour"), ("item", "hour"), ("ge", 3, 2)]),
        )


if __name__ == "__main__":
    unittest.main()
