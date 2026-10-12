"""Paired behavior for statically proved weather factors and best-hour lookup.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
Only proved class bodies execute, with explicit owned clocks and dependencies.
"""
import asyncio
from datetime import datetime, timezone
import math
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple
import unittest

from pure_reference import classes
from test_isolated_initialization import logger_for


WEATHER_MODULES = (
    "forecast/forecast_weather_calculator.py",
    "extra_features/solar_forecast_eai/forecast/forecast_weather_calculator.py",
)


def outcome(call):
    try:
        return call()
    except Exception as error:
        return type(error).__name__, str(error)


class WeatherValueModuleTests(unittest.TestCase):
    def calculator(self, relative, index, events, clock=None, season=None):
        globals_ = {
            "_LOGGER": logger_for(events), "math": math, "datetime": datetime,
            "Any": Any, "Dict": Dict, "Optional": Optional,
            "dt_util": clock, "get_season": season,
        }
        return classes(relative, "WeatherCalculator", globals_)[index]()

    def test_temperature_cloud_condition_and_sensor_boundaries(self):
        for relative in WEATHER_MODULES:
            results = []
            for index in (0, 1):
                events = []
                calculator = self.calculator(relative, index, events)
                values = [outcome(lambda value=value: calculator.get_temperature_factor(value))
                          for value in (None, -20, 0, 25, 26, 200, "bad")]
                values += [outcome(lambda cloud=cloud, condition=condition:
                                   calculator.get_cloud_factor(cloud, condition))
                           for cloud, condition in (
                               (None, None), (None, "RAINY"), (None, "fog"),
                               (None, "cloudy"), (None, "partlycloudy"),
                               (None, "sunny"), (None, "unknown"), (-10, None),
                               (0, None), (50, None), (100, None), (200, None),
                               ("bad", "rainy"), (None, 42))]
                values += [outcome(lambda condition=condition:
                                   calculator.get_condition_factor(condition))
                           for condition in (None, "rainy", "WINDY", "unknown", 3)]
                values += [calculator.calculate_effective_radiation(radiation, clouds, condition)
                           for radiation, clouds, condition in (
                               (-2, None, None), (0, 20, None), (1000, 50, None),
                               (500, None, "rainy"))]
                values += [outcome(lambda arguments=arguments:
                                   calculator.estimate_cloud_factor_from_sensors(*arguments))
                           for arguments in (
                               (None, None, None, None), (100, 200, None, None),
                               (200, 100, None, None), (-10, 100, None, None),
                               (100, 0, 20, 100), (None, None, 20, 100),
                               ("bad", 100, None, None))]
                results.append((values, events))
            self.assertEqual(results[1], results[0])
            self.assertEqual(results[1][0][:7], [0.9, 0.85, 0.85, 1.0, 0.996, 0.7, 0.9])
            self.assertEqual(results[1][0][-7:], [None, 0.5, 1.0, 0.0, 0.2, 0.2, None])

    def test_clock_conversion_seasonal_blending_and_error_fallbacks(self):
        for relative in WEATHER_MODULES:
            results = []
            for index in (0, 1):
                events = []
                fixed = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
                def current():
                    events.append("now")
                    return fixed
                def convert(kind, value):
                    events.append((kind, value.isoformat()))
                    return value.replace(tzinfo=timezone.utc)
                def season(month):
                    events.append(("season", month))
                    return ("winter" if month in (12, 1, 2) else
                            "spring" if month in (3, 4, 5) else
                            "summer" if month in (6, 7, 8) else "autumn")
                clock = SimpleNamespace(now=current,
                    ensure_local=lambda value: convert("ensure_local", value),
                    as_local=lambda value: convert("as_local", value))
                calculator = self.calculator(relative, index, events, clock, season)
                values = []
                for value in (None, datetime(2026, 6, 1),
                              datetime(2026, 10, 1, tzinfo=timezone.utc), "bad"):
                    values.extend((calculator.get_seasonal_adjustment(value),
                                   calculator.get_current_season(value)))
                values += [calculator.blend_cloud_factors(*arguments)
                           for arguments in ((0.6, 0.2, None, 0.7),
                               (None, 0.2, None, 0.7), (0.6, None, None, 0.7),
                               (None, None, "rainy", 0.7), (0.6, 0.2, "fog", "bad"))]
                values += [calculator.calculate_combined_weather_factor(data, include)
                           for data, include in (
                               ({"temperature": 25, "cloud_cover": 0, "condition": "sunny"}, False),
                               ({"temperature": 25, "clouds": 0, "condition": "sunny"}, True),
                               ({}, False), (None, True))]
                results.append((values, events))
            self.assertEqual(results[1], results[0])
            self.assertEqual(results[1][0][:8],
                             [0.35 * 0.85, "winter", 1.05, "summer", 0.65, "autumn", 0.65, "autumn"])
            self.assertEqual(results[1][0][-4:], [1.0, 0.35 * 0.85, 0.54, 0.5])

    def test_best_hour_database_empty_failure_and_cancellation(self):
        relative = "extra_features/solar_forecast_eai/ai/ai_best_hour.py"
        for mode in ("row", "empty", "none", "failure", "cancelled"):
            results = []
            for index in (0, 1):
                events = []
                async def fetch(statement, arguments):
                    events.append(("fetch", statement, arguments))
                    if mode == "failure":
                        raise RuntimeError("owned database error")
                    if mode == "cancelled":
                        raise asyncio.CancelledError("owned cancellation")
                    return [(13, 4.2)] if mode == "row" else [] if mode == "empty" else None
                fixed = datetime(2026, 10, 12, 9)
                cls = classes(relative, "BestHourCalculator", {
                    "_LOGGER": logger_for(events),
                    "datetime": SimpleNamespace(now=lambda: fixed),
                    "Any": Any, "Optional": Optional, "Tuple": Tuple,
                })[index]
                calculator = cls(SimpleNamespace(fetchall=fetch))
                try:
                    value = asyncio.run(calculator.calculate_best_hour_today())
                except asyncio.CancelledError:
                    value = "cancelled"
                results.append((value, events))
            self.assertEqual(results[1], results[0])
            self.assertEqual(results[1][0],
                             (13, 4.2) if mode == "row" else "cancelled" if mode == "cancelled" else (12, 0.0))
            self.assertEqual(results[1][1][0][2], ("2026-10-12",))


if __name__ == "__main__":
    unittest.main()
