"""Compare strict encoding contracts using owned payloads and policies.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No real license, integration module, runtime or external service is used.
"""

import base64
import json
from types import SimpleNamespace
import unittest

from pure_reference import functions


MODULES = (
    "extra_features/grid_price_monitor/license/codec.py",
    "extra_features/solar_forecast_eai/license/codec.py",
)
NAMES = ("canonical_payload", "encode_segment", "decode_segment", "compose_key")


def outcome(function, *arguments, **keywords):
    try:
        return ("value", function(*arguments, **keywords))
    except Exception as error:
        return (type(error).__name__, str(error))


class LicenseCodecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.codecs = []
        for relative in MODULES:
            policy = SimpleNamespace(MAX_PAYLOAD_BYTES=1000, PREFIX="OWNED")
            optional_prefix = "grid_price_monitor" in relative
            globals_ = {"base64": base64, "json": json}
            globals_.update(
                {"policy": policy}
                if optional_prefix
                else {"MAX_PAYLOAD_BYTES": 1000, "PREFIX": "OWNED"}
            )
            pair = functions(
                relative,
                NAMES,
                globals_,
                defaults={"compose_key": (None,)} if optional_prefix else None,
            )
            cls.codecs.append((relative, policy, pair))

    def test_canonical_payload_keeps_order_utf8_nested_values_and_errors(self):
        circular = {}
        circular["self"] = circular
        cases = (
            {},
            {"b": 2, "a": 1},
            {"accent": "é"},
            {"nested": [True, None, {"a": "value"}]},
            None,
            [2, 1],
            {"bad": {1}},
            {"number": float("nan")},
            {"number": float("inf")},
            {1: "numeric", "a": "text"},
            circular,
        )
        for relative, _, (original, recovered) in self.codecs:
            for payload in cases:
                with self.subTest(relative=relative, payload=repr(payload)):
                    self.assertEqual(
                        outcome(recovered["canonical_payload"], payload),
                        outcome(original["canonical_payload"], payload),
                    )
            self.assertEqual(
                recovered["canonical_payload"]({"b": 2, "a": "é"}),
                '{"a":"é","b":2}'.encode("utf-8"),
            )

    def test_base64_segments_keep_alphabet_empty_rule_and_exact_size_boundaries(self):
        values = (b"", b"a", b"ab", b"abc", bytes(range(256)))
        invalid = (
            None,
            "",
            [],
            "a",
            "YQ",
            "Y Q",
            "YQ=",
            "é",
            "___",
            "-_-",
            b"YQ",
            ("Y", "Q"),
        )
        for relative, _, (original, recovered) in self.codecs:
            for value in values:
                encoded = original["encode_segment"](value)
                self.assertEqual(recovered["encode_segment"](value), encoded)
                self.assertNotIn("=", encoded)
                for maximum in (0, len(value), len(value) - 1, len(value) + 1):
                    with self.subTest(relative=relative, value=value, maximum=maximum):
                        self.assertEqual(
                            outcome(
                                recovered["decode_segment"], encoded, maximum=maximum
                            ),
                            outcome(
                                original["decode_segment"], encoded, maximum=maximum
                            ),
                        )
                if value:
                    self.assertEqual(
                        recovered["decode_segment"](encoded, maximum=len(value)), value
                    )
            for value in invalid:
                for maximum in (0, 1, 2, -1):
                    with self.subTest(
                        relative=relative, invalid=value, maximum=maximum
                    ):
                        self.assertEqual(
                            outcome(
                                recovered["decode_segment"], value, maximum=maximum
                            ),
                            outcome(original["decode_segment"], value, maximum=maximum),
                        )
            self.assertEqual(
                outcome(recovered["decode_segment"], "", maximum=1000),
                ("ValueError", "invalid_base64url"),
            )

    def test_compose_key_keeps_payload_limit_prefix_and_argument_errors(self):
        from itertools import product

        for relative, policy, (original, recovered) in self.codecs:
            optional_prefix = "grid_price_monitor" in relative
            for maximum, payload, prefix, signature in product(
                (-1, 0, 3, 1000),
                ({}, {"x": "é"}, []),
                (None, "", False, "EXPLICIT") if optional_prefix else (None,),
                (b"owned", b"", None),
            ):
                policy.MAX_PAYLOAD_BYTES = maximum
                if not optional_prefix:
                    original["compose_key"].__globals__["MAX_PAYLOAD_BYTES"] = maximum
                    recovered["compose_key"].__globals__["MAX_PAYLOAD_BYTES"] = maximum
                arguments = (
                    (payload, signature, prefix)
                    if optional_prefix
                    else (payload, signature)
                )
                with self.subTest(
                    relative=relative,
                    maximum=maximum,
                    payload=payload,
                    prefix=prefix,
                    signature=signature,
                ):
                    self.assertEqual(
                        outcome(recovered["compose_key"], *arguments),
                        outcome(original["compose_key"], *arguments),
                    )
            policy.MAX_PAYLOAD_BYTES = 1000
            if not optional_prefix:
                original["compose_key"].__globals__["MAX_PAYLOAD_BYTES"] = 1000
                recovered["compose_key"].__globals__["MAX_PAYLOAD_BYTES"] = 1000
            self.assertEqual(
                recovered["compose_key"]({}, b"owned"), "OWNED.e30.b3duZWQ"
            )
            self.assertEqual(original["compose_key"]({}, b"owned"), "OWNED.e30.b3duZWQ")
            if not optional_prefix:
                self.assertEqual(
                    outcome(recovered["compose_key"], {}, b"owned", "EXPLICIT"),
                    outcome(original["compose_key"], {}, b"owned", "EXPLICIT"),
                )
                self.assertEqual(
                    outcome(recovered["compose_key"], {}, b"owned", "EXPLICIT")[0],
                    "TypeError",
                )

    def test_policy_reads_and_prefix_formatting_remain_in_order(self):
        def exercise(function, prefix_kind, failure):
            calls = []

            class Policy:
                @property
                def MAX_PAYLOAD_BYTES(self):
                    calls.append("maximum")
                    if failure == "maximum":
                        raise ValueError("maximum failed")
                    return 1000

                @property
                def PREFIX(self):
                    calls.append("prefix")
                    if failure == "prefix":
                        raise ValueError("prefix failed")
                    return Prefix()

            class Prefix:
                def __format__(self, spec):
                    calls.append(("format", spec))
                    if failure == "format":
                        raise ValueError("format failed")
                    return "OWNED"

            namespace = function.__globals__
            previous = namespace["policy"]
            namespace["policy"] = Policy()
            try:
                prefix = None if prefix_kind == "default" else Prefix()
                result = outcome(function, {}, b"owned", prefix)
            finally:
                namespace["policy"] = previous
            return result, calls

        for relative, _, (original, recovered) in self.codecs:
            if "grid_price_monitor" not in relative:
                continue
            for prefix in ("default", "explicit"):
                for failure in ("", "maximum", "prefix", "format"):
                    with self.subTest(
                        relative=relative, prefix=prefix, failure=failure
                    ):
                        self.assertEqual(
                            exercise(recovered["compose_key"], prefix, failure),
                            exercise(original["compose_key"], prefix, failure),
                        )

        def imported_policy(function, failure):
            calls = []

            class Limit:
                def __lt__(self, size):
                    calls.append(("maximum", size))
                    if failure == "maximum":
                        raise ValueError("maximum failed")
                    return size > 1000

            class Prefix:
                def __format__(self, spec):
                    calls.append(("format", spec))
                    if failure == "format":
                        raise ValueError("format failed")
                    return "OWNED"

            namespace = function.__globals__
            previous = namespace["MAX_PAYLOAD_BYTES"], namespace["PREFIX"]
            namespace["MAX_PAYLOAD_BYTES"], namespace["PREFIX"] = Limit(), Prefix()
            try:
                result = outcome(function, {}, b"owned")
            finally:
                namespace["MAX_PAYLOAD_BYTES"], namespace["PREFIX"] = previous
            return result, calls

        for relative, _, (original, recovered) in self.codecs:
            if "solar_forecast_eai" not in relative:
                continue
            for failure in ("", "maximum", "format"):
                self.assertEqual(
                    imported_policy(recovered["compose_key"], failure),
                    imported_policy(original["compose_key"], failure),
                )
            self.assertEqual(
                imported_policy(recovered["compose_key"], ""),
                (("value", "OWNED.e30.b3duZWQ"), [("maximum", 2), ("format", "")]),
            )


if __name__ == "__main__":
    unittest.main()
