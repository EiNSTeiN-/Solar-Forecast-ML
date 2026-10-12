"""Paired checks for new complete modules, with explicit owned dependencies.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No integration module, native PyArmor runtime or live Home Assistant is loaded.
"""

import ast
import asyncio
from datetime import datetime, timezone
import math
from pathlib import Path
from types import SimpleNamespace
import unittest

import numpy as np

from pure_reference import classes, functions
from test_isolated_initialization import logger_for


def literal_globals(relative):
    """Read scalar contract constants; never execute the module or its imports."""
    path = Path(__file__).resolve().parents[1] / "verified_units" / relative
    result = {}
    for node in ast.parse(path.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            try:
                result[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
    return result


class StatementScopeModuleTests(unittest.TestCase):
    def test_hubble_direct_and_cross_thread_routes_and_handled_failures(self):
        relative = "core/core_hubble.py"
        for mode in (
            "direct",
            "other_loop",
            "no_loop",
            "operation_error",
            "schedule_error",
            "cancelled",
        ):
            outputs = []
            for index in (0, 1):
                events = []

                def schedule(callback):
                    events.append(("schedule",))
                    if mode == "schedule_error":
                        raise RuntimeError("owned schedule")
                    callback()

                loop = SimpleNamespace(call_soon_threadsafe=schedule)

                def running_loop():
                    if mode == "no_loop":
                        raise RuntimeError("owned no loop")
                    if mode == "cancelled":
                        raise asyncio.CancelledError("owned cancellation")
                    return (
                        object() if mode in ("other_loop", "schedule_error") else loop
                    )

                def create(hass, **options):
                    events.append(("create", options))
                    if mode == "operation_error":
                        raise ValueError("owned create")

                def delete(hass, **options):
                    events.append(("delete", options))

                cls = classes(
                    relative,
                    "Hubble",
                    {
                        "asyncio": SimpleNamespace(get_running_loop=running_loop),
                        "_LOGGER": logger_for(events),
                        "Severity": SimpleNamespace(WARNING="warning"),
                        "async_create_issue": create,
                        "async_delete_issue": delete,
                    },
                )[index]
                hass = SimpleNamespace(loop=loop)
                try:
                    asyncio.run(
                        cls.async_create_issue(hass, "owned", "title", "description")
                    )
                    asyncio.run(cls.async_dismiss_issue(hass, "owned"))
                    outcome = "complete"
                except asyncio.CancelledError:
                    outcome = "cancelled"
                outputs.append((outcome, events))
            self.assertEqual(outputs[1], outputs[0])
            if mode == "cancelled":
                self.assertEqual(outputs[1][0], "cancelled")
            if mode == "other_loop":
                self.assertEqual(
                    sum(event[0] == "schedule" for event in outputs[1][1]), 2
                )

    def test_hubble_static_severity_routes(self):
        outcomes = []
        for index in (0, 1):
            events = []
            cls = classes(
                "core/core_hubble.py",
                "Hubble",
                {
                    "_LOGGER": logger_for(events),
                    "Severity": SimpleNamespace(WARNING="warning"),
                },
            )[index]
            cls.info("owned information")
            cls.warning("owned warning")
            cls.error("owned error")
            outcomes.append(events)
        self.assertEqual(outcomes[1], outcomes[0])
        self.assertEqual(
            [event[1] for event in outcomes[1]], ["info", "warning", "error"]
        )

    def learning_functions(self, events):
        relative = "data/data_learning_filter.py"
        names = [
            "throttle_reason_excludes_from_learning",
            "production_miss_uplift",
            "is_learnable_weather_miss",
            "weather_alert_excludes_from_learning",
            "weather_alert_excludes_from_clean_evaluation",
            "should_exclude_hour_from_learning",
            "hour_is_learnable",
            "filter_predictions_for_learning",
            "calculate_excluded_hours_ratio",
            "should_skip_daily_learning",
            "_get_sample_id",
            "should_exclude_group_from_learning",
            "should_skip_daily_learning_per_group",
        ]
        globals_ = dict(
            literal_globals(relative),
            _LOGGER=logger_for(events),
            CURTAILMENT_THROTTLE_REASONS={"owned_limit"},
            LEARNABLE_WEATHER_MISS_ALERT_TYPES=frozenset(
                {"unexpected_rain", "unexpected_snow", "unexpected_clouds"}
            ),
            QUALITY_EXCLUDED_WEATHER_ALERT_TYPES=frozenset({"snow_covered_panels"}),
        )
        defaults = {
            "should_exclude_hour_from_learning": ("general",),
            "is_learnable_weather_miss": (None,),
            "weather_alert_excludes_from_learning": (None,),
            "calculate_excluded_hours_ratio": (True,),
            "should_skip_daily_learning": (0.25,),
            "should_exclude_group_from_learning": (None, None),
        }
        return functions(relative, names, globals_, defaults=defaults)

    def test_learning_keeps_actual_only_or_prediction_only_hours_and_exclusion_reasons(
        self,
    ):
        outputs = []
        for index in (0, 1):
            events = []
            f = self.learning_functions(events)[index]
            rows = [
                dict(actual_kwh=1, prediction_kwh=0, flags={"inverter_clipped": True}),
                dict(actual_kwh=0, prediction_kwh=1, flags={}),
                dict(
                    actual_kwh=0,
                    prediction_kwh=0,
                    flags={"exclude_from_learning": True},
                ),
            ]
            outputs.append(
                (
                    f["calculate_excluded_hours_ratio"](rows),
                    f["calculate_excluded_hours_ratio"](rows, False),
                    f["filter_predictions_for_learning"](rows, "general", True),
                    f["should_skip_daily_learning"](rows, 0.25),
                    events,
                )
            )
        self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(outputs[1][0], (0.5, 1, 2))
        self.assertEqual(outputs[1][1], (2 / 3, 2, 3))
        self.assertEqual(
            outputs[1][2][1], {"inverter_clipped": 1, "manually_excluded": 1}
        )

    def test_learning_weather_classes_and_panel_group_isolation(self):
        outputs = []
        for index in (0, 1):
            events = []
            f = self.learning_functions(events)[index]
            weather = []
            for alert in (
                None,
                "",
                "unexpected_rain",
                "unexpected_sun",
                "snow_covered_panels",
            ):
                for miss_class in (
                    None,
                    "forecast_miss",
                    "legacy_forecast_miss",
                    "technical",
                ):
                    weather.append(
                        (
                            f["weather_alert_excludes_from_learning"](
                                alert, miss_class
                            ),
                            f["weather_alert_excludes_from_clean_evaluation"](alert),
                        )
                    )
            row = dict(actual_kwh=1, prediction_kwh=1, flags={})
            group_results = [
                f["should_exclude_group_from_learning"](
                    row,
                    group,
                    {"east": {"snow_covered": True}},
                    {"west": {"shadow_type": "possible_obstruction"}},
                )
                for group in ("east", "west", "south")
            ]
            uplifts = [
                f["production_miss_uplift"](actual, predicted)
                for actual, predicted in (
                    (None, 1),
                    ("invalid", 1),
                    (1, 0.01),
                    (1, 1),
                    (3, 1),
                    (0.3, 0.15),
                )
            ]
            outputs.append((weather, group_results, uplifts, events))
        self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(
            outputs[1][1],
            [
                (True, "snow_covered_panels"),
                (True, "possible_obstruction"),
                (False, ""),
            ],
        )
        self.assertEqual(outputs[1][2], [None, None, None, None, 1.35, 1.35])

    def test_frost_forecast_and_physical_boundaries(self):
        self.check_frost_forecast_and_physical_boundaries(
            "data/data_frost_detection.py"
        )

    def test_eai_frost_forecast_and_physical_boundaries(self):
        self.check_frost_forecast_and_physical_boundaries(
            "extra_features/solar_forecast_eai/data/data_frost_detection.py"
        )

    def check_frost_forecast_and_physical_boundaries(self, relative):
        outputs = []
        for index in (0, 1):
            events = []
            cls = classes(
                relative,
                "FrostDetector",
                {"math": math, "_LOGGER": logger_for(events)},
                bases=(object,),
            )[index]
            f = functions(
                relative,
                ["estimate_frost_from_forecast"],
                {"math": math, "FrostDetector": cls},
            )[index]
            forecast = [
                f["estimate_frost_from_forecast"](temp, humidity, wind, 7)
                for temp in (None, -10, -3, -1, 0, 2, 4)
                for humidity in (None, 0, 50, 90, 100)
                for wind in (None, 0, 2, 5)
            ]
            detector = cls.__new__(cls)
            physics = [
                (
                    detector._calculate_dewpoint(temp, humidity),
                    detector._calculate_wind_frost_factor(wind),
                )
                for temp, humidity, wind in ((-5, 90, 0), (2, 0, 5), (10, 50, 2))
            ]
            outputs.append((forecast, physics, events))
        self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(outputs[1][0][0], (None, None))

    def test_feature_importance_permutations_callbacks_and_error_routes(self):
        self.check_feature_importance_permutations_callbacks_and_error_routes(
            "ai/ai_feature_importance.py"
        )

    def test_eai_feature_importance_permutations_callbacks_and_error_routes(self):
        self.check_feature_importance_permutations_callbacks_and_error_routes(
            "extra_features/solar_forecast_eai/ai/ai_feature_importance.py"
        )

    def check_feature_importance_permutations_callbacks_and_error_routes(
        self, relative
    ):
        class Result(SimpleNamespace):
            def to_dict(self):
                return vars(self).copy()

        for failure in (None, "save", "callback", "cancel"):
            outputs = []
            for index in (0, 1):
                events = []

                async def sleep(seconds):
                    events.append(("sleep", seconds))

                async def save(value):
                    events.append(("save", value))
                    if failure == "save":
                        raise ValueError("owned save")

                async def progress(*values):
                    events.append(("progress", values))
                    if failure == "callback":
                        raise ValueError("owned callback")
                    if failure == "cancel":
                        raise asyncio.CancelledError("owned cancellation")

                cls = classes(
                    relative,
                    "FeatureImportanceAnalyzer",
                    {
                        "_ensure_numpy": lambda: np,
                        "_LOGGER": logger_for(events),
                        "asyncio": SimpleNamespace(sleep=sleep),
                        "FeatureImportanceResult": Result,
                        "datetime": SimpleNamespace(
                            now=lambda: datetime(2026, 10, 12, tzinfo=timezone.utc)
                        ),
                        "SafeDateTimeUtil": SimpleNamespace(
                            now=lambda: datetime(2026, 10, 12, tzinfo=timezone.utc)
                        ),
                    },
                )[index]
                analyzer = cls(
                    SimpleNamespace(save_feature_importance=save), num_permutations=2
                )
                model = SimpleNamespace(
                    predict=lambda sequence: [float(sequence[0, 0])]
                )
                x = [np.full((3, 2), n / 10) for n in range(10)]
                np.random.seed(1729)
                try:
                    result = vars(
                        asyncio.run(
                            analyzer.analyze(
                                model,
                                x,
                                [n / 10 for n in range(10)],
                                ["a", "b"],
                                progress,
                            )
                        )
                    )
                except (ValueError, asyncio.CancelledError) as error:
                    result = (type(error).__name__, str(error))
                outputs.append((result, events))
            self.assertEqual(outputs[1], outputs[0])
            if failure in (None, "save"):
                self.assertTrue(outputs[1][0]["success"])
            if failure == "cancel":
                self.assertEqual(outputs[1][0][0], "CancelledError")

    def test_ridge_temporal_holdout_training_persistence_and_cancellation(self):
        relative = "ai/ai_tiny_ridge.py"
        for cancel in (False, True):
            outcomes = []
            for index in (0, 1):
                events = []

                async def sleep(seconds):
                    events.append(("sleep", seconds))
                    if cancel:
                        raise asyncio.CancelledError("owned training")

                cls = classes(
                    relative,
                    "TinyRidge",
                    {
                        "_ensure_numpy": lambda: np,
                        "_LOGGER": logger_for(events),
                        "asyncio": SimpleNamespace(sleep=sleep),
                    },
                )[index]
                model = cls(input_size=1, sequence_length=2)
                x = [
                    np.array([[number / 10], [number / 10 + 0.1]])
                    for number in range(24)
                ]
                y = [number / 5 + 0.2 for number in range(24)]
                split = cls._chronological_split_indices(24, 0.25)
                try:
                    training = asyncio.run(
                        model._train_in_process(x, y, validation_split=0.25)
                    )
                except asyncio.CancelledError:
                    training = "cancelled"
                weights = model.get_weights()
                prediction = model.predict(x[-1]) if model.is_trained else None
                if model.is_trained:
                    model.set_weights(weights, strict=True)
                outcomes.append((split, training, weights, prediction, events))
            self.assertEqual(outcomes[1], outcomes[0])
            self.assertEqual(outcomes[1][0], (list(range(18)), list(range(18, 24))))
            if cancel:
                self.assertEqual(outcomes[1][1], "cancelled")
            else:
                self.assertTrue(outcomes[1][1]["success"])
                self.assertEqual(outcomes[1][1]["holdout_samples"], 6)

    def test_main_weather_mlp_gradients_and_day_blocked_quality_contracts(self):
        relative = "ai/ai_weather_mlp.py"
        helper_names = [
            "day_blocked_folds",
            "held_out_training_shortfall",
            "pooled_evaluation_metrics",
            "weather_mlp_persist_verdict",
        ]
        outcomes = []
        for index in (0, 1):
            events = []
            globals_ = dict(
                literal_globals(relative),
                _ensure_numpy=lambda: np,
                math=math,
                _LOGGER=logger_for(events),
            )
            helpers = functions(relative, helper_names, globals_)[index]
            globals_.update(helpers)
            cls = classes(relative, "TinyWeatherMLP", globals_)[index]
            np.random.seed(1729)
            model = cls(hidden1=4, hidden2=3)
            x = np.arange(12, dtype=np.float64) / 10
            prediction, cache = model.forward(x)
            gradients = {
                name: value.tolist()
                for name, value in model.backward(cache, 1.1, prediction).items()
            }
            loss = model.train_step(x, 1.1)
            weights = model.get_weights()
            dates = [f"2026-10-{day:02d}" for day in range(1, 7) for _ in range(12)]
            folds = helpers["day_blocked_folds"](dates, 3)
            shortfall = helpers["held_out_training_shortfall"](dates, 60, 3)
            metrics = helpers["pooled_evaluation_metrics"]([0.5, 1, 1.5], [0.5, 1, 1.5])
            verdict = helpers["weather_mlp_persist_verdict"](metrics)
            quality = []
            for samples, accuracy, rmse in (
                (0, 0.9, 0),
                (100, 0.1, 0.1),
                (100, 0.7, 0.1),
            ):
                model.trained_samples, model.training_accuracy, model.training_rmse = (
                    samples,
                    accuracy,
                    rmse,
                )
                quality.append(
                    (model.is_usable, model.is_trained, model.application_confidence)
                )
            outcomes.append(
                (
                    prediction,
                    gradients,
                    loss,
                    weights,
                    folds,
                    shortfall,
                    metrics,
                    verdict,
                    quality,
                    events,
                )
            )
        self.assertEqual(outcomes[1], outcomes[0])
        self.assertEqual(outcomes[1][5], 48)
        self.assertEqual(outcomes[1][6]["r2"], 1.0)
        self.assertIsNone(outcomes[1][7])
        for training, validation in outcomes[1][4]:
            self.assertTrue(set(training).isdisjoint(validation))


if __name__ == "__main__":
    unittest.main()
