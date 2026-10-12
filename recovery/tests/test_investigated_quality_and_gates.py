"""Paired pure quality/gate tests with controlled dependencies.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No integration module or vendor runtime is imported.
"""

import ast
import asyncio
from collections import defaultdict
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
from statistics import fmean
from types import SimpleNamespace
import unittest
from pure_reference import functions

BASE = "extra_features/sfml_stats/core/"


def names(relative):
    tree = ast.parse(
        (Path(__file__).resolve().parents[1] / "verified_units" / relative).read_text()
    )
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def quality_pair():
    relative = BASE + "forecast_quality.py"
    globals_ = dict(
        math=math,
        defaultdict=defaultdict,
        date=date,
        timedelta=timedelta,
        fmean=fmean,
        Mapping=Mapping,
        Sequence=Sequence,
        PRODUCTION_CANDIDATE_KWH_THRESHOLD=0.01,
        MIN_VALID_DAY_HOURS=4,
        MIN_VALID_DAY_COVERAGE=40.0,
        MIN_HEALTH_DAYS=7,
        MIN_MODEL_POINTS=24,
        MIN_MODEL_DAYS=3,
        MODEL_TIE_MAE_KWH=0.01,
        VISIBLE_WEATHER_MISS_TYPES=frozenset(
            ("unexpected_rain", "unexpected_snow", "unexpected_clouds")
        ),
        HARD_WEATHER_EXCLUSION_TYPES=frozenset(("snow_covered_panels", "manual_pause")),
    )
    return functions(
        relative,
        names(relative),
        globals_,
        defaults={"_value": (None,), "_clamp": (0.0, 100.0), "_round": (1,)},
        kwdefaults={
            "build_health_dashboard": {"model_battle": None},
            "calculate_model_battle": {"period": None, "mode": "morning"},
        },
    )


class ForecastQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pair = quality_pair()

    def test_validation_numeric_coverage_and_exclusion_boundaries(self):
        cases = [None, True, False, 0, -1, "4.5", "bad", float("nan"), float("inf")]
        for value in cases:
            for name in ("_number",):
                self.assertEqual(self.pair[1][name](value), self.pair[0][name](value))
        base = {
            "evaluation_hours_count": 4,
            "production_candidate_hours_count": 10,
            "evaluation_coverage_percent": 40,
            "accuracy_percent": 95,
            "final": 2,
            "actual": 1,
        }
        variants = [
            base,
            {},
            dict(base, evaluation_hours_count=3),
            dict(base, evaluation_coverage_percent=39),
            dict(base, final=None),
            dict(base, mppt_throttled=True),
            dict(base, inverter_clipped=True),
            dict(base, exclude_from_learning=True),
        ]
        for alert in ("", "unexpected_rain", "snow_covered_panels", "manual_pause"):
            variants.append(
                dict(base, weather_alert_type=alert, exclude_from_clean_evaluation=True)
            )
        for row in variants:
            for name in (
                "daily_evaluation_hours",
                "daily_candidate_hours",
                "daily_coverage",
                "is_valid_day",
                "is_valid_hour",
            ):
                self.assertEqual(self.pair[1][name](row), self.pair[0][name](row))
        self.assertTrue(self.pair[1]["is_valid_day"](base))
        self.assertFalse(
            self.pair[1]["is_valid_day"](dict(base, evaluation_hours_count=3))
        )

    def daily_rows(self, count=21):
        return [
            {
                "date": (date(2026, 10, 1) + timedelta(days=index)).isoformat(),
                "evaluation_hours_count": 8,
                "production_candidate_hours_count": 8,
                "evaluation_coverage_percent": 100,
                "accuracy_percent": 95,
                "evaluation_predicted_kwh": 10 + index / 10,
                "evaluation_actual_kwh": 9 + index / 10,
            }
            for index in range(count)
        ]

    def test_health_period_heatmap_streak_and_weekly_ties(self):
        rows = self.daily_rows()
        for sample in (
            [],
            rows[:3],
            rows,
            rows[::2],
            rows + [dict(rows[-1], accuracy_percent=None)],
        ):
            calls = [
                ("aggregate_daily_period", {}),
                (
                    "build_health_dashboard",
                    {"period_days": 7, "calculated_at": "owned"},
                ),
                (
                    "build_heatmap",
                    {"start": date(2026, 10, 1), "end": date(2026, 10, 23)},
                ),
                ("build_milestones", {}),
                ("build_trends", {}),
            ]
            for name, kwargs in calls:
                self.assertEqual(
                    self.pair[1][name](sample, **kwargs),
                    self.pair[0][name](sample, **kwargs),
                )
        dashboard = self.pair[1]["build_health_dashboard"](
            rows, period_days=7, calculated_at="owned"
        )
        self.assertTrue(dashboard["health"]["available"])
        self.assertEqual(dashboard["health"]["valid_days"], 7)
        self.assertEqual(
            dashboard["health"]["period"], {"start": "2026-10-15", "end": "2026-10-21"}
        )
        self.assertEqual(
            self.pair[1]["build_heatmap"](
                rows, start=date(2026, 10, 21), end=date(2026, 10, 22)
            )["days"][1]["state"],
            "missing",
        )

    def test_model_battle_common_points_winners_and_ties(self):
        rows = [
            {
                "date": f"2026-10-{day:02d}",
                "eligible": True,
                "actual": float(hour + 1),
                "final": float(hour + 2),
                "physics": float(hour + 1),
                "ai": float(hour + 3),
            }
            for day in range(1, 4)
            for hour in range(8)
        ]
        cases = (
            [],
            rows[:23],
            rows,
            [dict(row, final=row["physics"], ai=row["physics"]) for row in rows],
            [
                dict(row, physics=None) if index % 2 else row
                for index, row in enumerate(rows)
            ],
            [dict(row, eligible=False) for row in rows],
        )
        for sample in cases:
            self.assertEqual(
                self.pair[1]["calculate_model_battle"](sample),
                self.pair[0]["calculate_model_battle"](sample),
            )
        result = self.pair[1]["calculate_model_battle"](rows)
        self.assertEqual(
            (result["available"], result["winner"], result["common_points"]),
            (True, "physics", 24),
        )
        self.assertFalse(self.pair[1]["calculate_model_battle"](rows[:23])["available"])

    def test_replay_keeps_order_cumulative_totals_and_absent_series(self):
        rows = [
            {"hour": 2, "actual": 3, "final": 2.5, "eligible": True},
            {"hour": 1, "actual": 2, "final": 3, "eligible": True},
            {"hour": 3, "actual": None, "final": None, "eligible": False},
        ]
        for sample in ([], rows, rows[:1]):
            self.assertEqual(
                self.pair[1]["build_replay"](sample, replay_date="2026-10-12"),
                self.pair[0]["build_replay"](sample, replay_date="2026-10-12"),
            )
        result = self.pair[1]["build_replay"](rows, replay_date="2026-10-12")
        self.assertEqual([row["hour"] for row in result["hours"]], [1, 2, 3])
        self.assertEqual(result["hours"][-1]["cumulative_actual_kwh"], 5)
        self.assertFalse(result["series_available"]["p10"])


class GridChargeGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        relative = BASE + "grid_charge_gate.py"
        cls.pair = functions(
            relative,
            names(relative),
            dict(datetime=datetime, isfinite=math.isfinite, Mapping=Mapping),
            defaults={"classify_snapshot": (None,), "grid_charge_allowed": (None,)},
        )
        cls.now = datetime(2026, 10, 12, 12, 30, tzinfo=timezone(timedelta(hours=-4)))

    def test_price_freshness_force_priority_and_cheap_hour_modes(self):
        data = {
            "last_update": self.now.astimezone(timezone.utc).isoformat(),
            "total_price": 10,
            "threshold_mode": "absolute",
            "effective_threshold_ct": 11,
            "thresholds": {"force_charge_price": 5},
        }
        variants = [
            None,
            {},
            data,
            dict(data, is_demo=True),
            dict(data, total_price=None),
            dict(data, last_update=(self.now - timedelta(hours=1)).isoformat()),
            dict(data, thresholds={"force_charge_price": 12}),
            dict(data, threshold_mode="cheapest_hours", cheap_hours_today=[12, "13"]),
            dict(data, threshold_mode="cheapest_hours", cheap_hours_today=["bad"]),
            dict(data, effective_threshold_ct=10),
        ]
        coordinator = SimpleNamespace(
            entry=SimpleNamespace(
                data={"force_charge_price": 99}, options={"force_charge_price": 15}
            )
        )
        for sample in variants:
            for name in ("classify_snapshot", "grid_charge_allowed"):
                self.assertEqual(
                    self.pair[1][name](sample, self.now, coordinator),
                    self.pair[0][name](sample, self.now, coordinator),
                )
        self.assertEqual(
            self.pair[1]["classify_snapshot"](data, self.now, coordinator),
            (True, False),
        )
        self.assertEqual(
            self.pair[1]["classify_snapshot"](
                dict(data, thresholds={"force_charge_price": None}),
                self.now,
                coordinator,
            ),
            (True, False),
        )

    def test_live_source_skips_non_entries_before_stats_fallback(self):
        payload = {"last_update": self.now.isoformat(), "total_price": 3}
        coordinator = SimpleNamespace(data=payload)
        variants = (
            None,
            SimpleNamespace(data=None),
            SimpleNamespace(data={}),
            SimpleNamespace(
                data={"grid_price_monitor": {"bad": object(), "good": coordinator}}
            ),
            SimpleNamespace(
                data={
                    "sfml_stats": {
                        "bad": object(),
                        "missing": {},
                        "good": {"gpm_coordinator": coordinator},
                    }
                }
            ),
        )
        for hass in variants:
            for name in (
                "coordinator_price_data",
                "resolve_grid_charge",
                "resolve_hour_is_cheap",
            ):
                args = (hass,) if name == "coordinator_price_data" else (hass, self.now)
                self.assertEqual(self.pair[1][name](*args), self.pair[0][name](*args))
        self.assertIs(self.pair[1]["coordinator_price_data"](variants[-1]), payload)


class SMCOriginTests(unittest.TestCase):
    def test_writer_origin_success_errors_cancellation_and_unbound_writer(self):
        pair = functions(
            BASE + "smc_enable.py",
            ["current_origin", "bind_enable_writer", "async_set_enabled"],
            {
                "_origin": ContextVar("owned-origin", default="external"),
                "_writer": None,
                "EMS_ORIGIN": "ems",
            },
        )
        for api in pair:
            with self.assertRaisesRegex(RuntimeError, "not bound"):
                asyncio.run(api["async_set_enabled"](object(), True))
        for failure in ("", "error", "cancel"):
            outputs = []
            for api in pair:
                calls = []

                async def writer(hass, enabled):
                    calls.append((api["current_origin"](), enabled))
                    await asyncio.sleep(0)
                    calls.append(api["current_origin"]())
                    if failure == "error":
                        raise ValueError("owned writer failed")
                    if failure == "cancel":
                        raise asyncio.CancelledError("owned writer cancelled")
                    return {"enabled": enabled}

                api["bind_enable_writer"](writer)

                async def run():
                    try:
                        result = await api["async_set_enabled"](object(), 1)
                    except (ValueError, asyncio.CancelledError) as error:
                        result = (type(error).__name__, str(error))
                    return result, calls, api["current_origin"]()

                outputs.append(asyncio.run(run()))
            self.assertEqual(outputs[1], outputs[0])
            self.assertEqual(outputs[0][1:], ([("ems", True), "ems"], "external"))

    def test_concurrent_tasks_do_not_leak_origin_to_external_caller(self):
        pair = functions(
            BASE + "smc_enable.py",
            ["current_origin", "bind_enable_writer", "async_set_enabled"],
            {
                "_origin": ContextVar("owned-origin", default="external"),
                "_writer": None,
                "EMS_ORIGIN": "ems",
            },
        )
        outputs = []
        for api in pair:

            async def run():
                entered, release = asyncio.Event(), asyncio.Event()

                async def writer(hass, enabled):
                    entered.set()
                    await release.wait()
                    return {"origin": api["current_origin"](), "enabled": enabled}

                api["bind_enable_writer"](writer)
                task = asyncio.create_task(api["async_set_enabled"](object(), False))
                await entered.wait()
                outside = api["current_origin"]()
                release.set()
                return outside, await task, api["current_origin"]()

            outputs.append(asyncio.run(run()))
        self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(
            outputs[0], ("external", {"origin": "ems", "enabled": False}, "external")
        )
