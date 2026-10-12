"""Paired configuration and weather contracts with owned dependencies.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No Home Assistant integration, native runtime or real database is imported.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
import unittest
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pure_reference import classes

CONFIGURATION = "core/core_coordinator_init_helpers.py"
WEATHER = "data/sfml_weather_provider.py"
NOW = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class Logger:
    def __init__(self):
        self.calls = []

    def warning(self, *args):
        self.calls.append(("warning", args))

    def info(self, *args):
        self.calls.append(("info", args))


class ConfigurationTests(unittest.TestCase):
    def pair(self):
        self.logger = Logger()
        self.groups = []

        def normalize(group):
            self.groups.append(dict(group))
            return {**group, "owned_normalized": True}

        constants = {
            "CONF_" + name.upper(): name
            for name in (
                "solar_capacity",
                "learning_enabled",
                "hourly",
                "weather_entity",
                "total_consumption_today",
                "panel_groups",
                "inverter_max_power",
            )
        }
        return classes(
            CONFIGURATION,
            "CoordinatorInitHelpers",
            {
                **constants,
                "DEFAULT_SOLAR_CAPACITY": 10.0,
                "DEFAULT_INVERTER_MAX_POWER": 0.0,
                "DOMAIN": "solar_forecast_ml",
                "ConfigEntry": object,
                "HomeAssistant": object,
                "Path": Path,
                "CoordinatorConfiguration": lambda **values: SimpleNamespace(**values),
                "normalize_panel_group_capacity_fields": normalize,
                "_LOGGER": self.logger,
            },
        )

    def test_capacity_precedence_group_filtering_clipping_options_and_errors(self):
        pair = self.pair()
        for capacity in (None, 0, 4.32, -1, "bad"):
            for clipping in (None, 0, 12.0):
                results = []
                for cls in pair:
                    self.logger.calls.clear()
                    self.groups.clear()
                    entry = SimpleNamespace(
                        data={
                            "solar_capacity": capacity,
                            "plant_kwp": 6.4,
                            "panel_groups": [{"name": "east"}, None, {"name": "south"}],
                            "inverter_max_power": clipping,
                            "weather_entity": "weather.owned",
                        },
                        options={"learning_enabled": False, "hourly": True},
                    )
                    try:
                        value = cls.extract_configuration(entry)
                        self.assertEqual(
                            value.solar_capacity,
                            6.4 if capacity in (None, 0) else capacity,
                        )
                        self.assertFalse(value.learning_enabled)
                        self.assertTrue(value.enable_hourly)
                        self.assertEqual(value.primary_weather_entity, "weather.owned")
                        self.assertEqual(len(value.panel_groups), 2)
                        self.assertEqual(value.inverter_max_power, float(clipping or 0))
                        result = vars(value)
                    except ValueError as error:
                        self.assertEqual(capacity, "bad")
                        result = (type(error).__name__, str(error))
                    results.append((result, list(self.groups), list(self.logger.calls)))
                self.assertEqual(results[1], results[0])

    def test_default_configuration_directory_and_dataclass_properties(self):
        pair = self.pair()
        defaults = []
        for cls in pair:
            value = cls.extract_configuration(SimpleNamespace(data={}, options={}))
            self.assertEqual(value.solar_capacity, 10.0)
            self.assertTrue(value.learning_enabled)
            self.assertFalse(value.enable_hourly)
            self.assertEqual(value.panel_groups, [])
            self.assertEqual(value.inverter_max_power, 0.0)
            defaults.append(vars(value))
        self.assertEqual(defaults[1], defaults[0])
        for path in ("/owned/config", "relative", None):
            results = []
            for cls in pair:
                calls = []
                hass = SimpleNamespace(
                    config=SimpleNamespace(path=lambda: calls.append("path") or path)
                )
                try:
                    result = cls.setup_data_directory(hass)
                    self.assertEqual(result, Path(path) / "solar_forecast_ml")
                except TypeError as error:
                    self.assertIsNone(path)
                    result = (type(error).__name__, str(error))
                results.append((result, calls))
            self.assertEqual(results[1], results[0])
        configurations = classes(
            CONFIGURATION,
            "CoordinatorConfiguration",
            {
                "Any": Any,
                "Optional": Optional,
                "field": lambda **kwargs: SimpleNamespace(**kwargs),
            },
        )
        for panels in ([], [{"name": "east"}]):
            for clipping in (0, 1, -1):
                outputs = []
                for cls in configurations:
                    value = cls.__new__(cls)
                    value.panel_groups = panels
                    value.inverter_max_power = clipping
                    outputs.append(
                        (value.has_panel_groups, value.has_inverter_clipping)
                    )
                self.assertEqual(outputs, [(bool(panels), clipping > 0)] * 2)


def daily(period, minimum, maximum, average, count, rain, radiation=None):
    return {
        "period": period,
        "values": {
            "temperature": {
                "min": minimum,
                "max": maximum,
                "avg": average,
                "count": count,
            }
        },
        "rain_total_mm": rain,
        "radiation_avg_wm2": radiation,
        "radiation_max_wm2": None if radiation is None else radiation * 2,
        "lux_max": None,
    }


class WeatherContractTests(unittest.TestCase):
    def pair(self, timeout=8):
        return classes(
            WEATHER,
            "SFMLWeatherIntelligenceProvider",
            {
                "Any": Any,
                "asyncio": asyncio,
                "datetime": Clock,
                "timedelta": timedelta,
                "timezone": timezone,
                "ZoneInfo": ZoneInfo,
                "ZoneInfoNotFoundError": ZoneInfoNotFoundError,
                "_MAX_HISTORY_DAYS": 731,
                "_QUERY_TIMEOUT_SECONDS": timeout,
            },
        )

    def test_timezones_dst_roundtrips_and_timestamp_validation(self):
        for zone in (None, "UTC", "America/Toronto", "owned/invalid"):
            providers = [cls(None, zone) for cls in self.pair()]
            for date, hour in (("2026-10-12", 8), ("2026-03-08", 2), ("2026-11-01", 1)):
                values = [p._utc_at(date, hour) for p in providers]
                self.assertEqual(values[1], values[0])
                if zone == "America/Toronto" and date == "2026-03-08":
                    self.assertEqual(values, [None, None])
            for value in (
                None,
                4,
                "bad",
                "2026-10-12T08:00:00",
                "2026-10-12T08:00:00Z",
            ):
                self.assertEqual(
                    providers[1]._utc_timestamp(value),
                    providers[0]._utc_timestamp(value),
                )

    def test_row_serializers_numeric_filters_rounding_and_missing_values(self):
        rows = (
            {},
            {
                "period": "2026-10-12",
                "temperature_avg": 12.34567,
                "temperature_count": 3,
                "rain_total": 0.12345,
                "humidity_avg": "70",
            },
            {
                "timestamp": "2026-10-12T08:00:00Z",
                "temperature_c": 10,
                "wind_speed_ms": 2.3456,
                "source": "owned",
                "lux": False,
            },
            {
                "prediction_id": "2026-10-12_08",
                "temperature": 10.3,
                "wind": 4,
                "source": "owned",
                "rain": "bad",
            },
        )
        providers = [cls(None, "UTC") for cls in self.pair()]
        for row in rows:
            for method in (
                "_daily_row",
                "_hourly_actual_row",
                "_actual_row",
                "_kepler_row",
            ):
                outputs = []
                for provider in providers:
                    try:
                        outputs.append(getattr(provider, method)(row))
                    except ValueError as error:
                        self.assertEqual(method, "_kepler_row")
                        outputs.append((type(error).__name__, str(error)))
                self.assertEqual(outputs[1], outputs[0])
        value = providers[0]._daily_row(rows[1])
        self.assertEqual(value["values"]["temperature"]["avg"], 12.346)
        self.assertIsNone(value["values"]["humidity"]["avg"])
        self.assertEqual(
            providers[0]._hourly_actual_row(rows[2])["values"]["wind_speed"], 8.444
        )

    def test_weighted_summary_rain_records_months_years_and_leap_day_fallback(self):
        rows = [
            daily("2025-02-28", 1, 5, 3, 2, 0),
            daily("2026-10-10", 2, 8, 5, 3, 0.1, 100),
            daily("2026-10-11", 3, 9, 6, 1, 0.2, 200),
            daily("2026-10-12", 4, 10, None, 0, None),
        ]
        pair = self.pair()
        for data in ([], rows[:1], rows):
            outputs = []
            for cls in pair:
                outputs.append(
                    (
                        cls._summary(data, NOW - timedelta(days=4), NOW),
                        cls._precipitation(data),
                        cls._records(data),
                        cls._monthly(data),
                        cls._year_comparison(data, "2024-02-29"),
                    )
                )
            self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(pair[0]._precipitation(rows)["dry_spell_days"], 2)
        self.assertEqual(
            pair[0]._year_comparison(rows, "2024-02-29")["same_day_last_year"]["date"],
            "2023-02-28",
        )
        self.assertEqual(
            pair[0]._summary(rows, NOW - timedelta(days=4), NOW)["temperature_avg_c"],
            4.5,
        )

    def test_snapshot_query_contract_override_order_and_empty_availability(self):
        for empty in (False, True):
            outputs = []
            for cls in self.pair():
                queries = []
                batches = [
                    [
                        {
                            "period": "2026-10-12",
                            "temperature_avg": 1,
                            "temperature_count": 1,
                        }
                    ],
                    [
                        {
                            "period": "2026-10-12",
                            "temperature_avg": 10,
                            "temperature_count": 1,
                            "rain_total": 0.3,
                        }
                    ],
                    [{"timestamp": "2026-10-12T08:00:00Z", "temperature_c": 12}],
                    [
                        {
                            "forecast_date": "2026-10-12",
                            "hour": 8,
                            "direct_radiation": 200,
                        }
                    ],
                    [{"prediction_id": "2026-10-12_08", "temperature": 11}],
                ]

                async def fetch_all(sql, parameters):
                    index = len(queries)
                    queries.append((sql, parameters))
                    await asyncio.sleep(0)
                    return [] if empty else batches[index]

                provider = cls(SimpleNamespace(fetch_all=fetch_all), "UTC")
                result = asyncio.run(provider.snapshot(NOW - timedelta(days=1000), NOW))
                self.assertEqual(len(queries), 5)
                self.assertEqual(
                    queries[0][1],
                    (
                        (NOW - timedelta(days=731)).date().isoformat() + "_00",
                        "2026-10-12_23",
                    ),
                )
                self.assertEqual(result["available"], not empty)
                self.assertEqual(
                    result["status"], "unavailable" if empty else "available"
                )
                if not empty:
                    self.assertEqual(result["summary"]["temperature_avg_c"], 10)
                    self.assertEqual(
                        result["aggregates"]["data"][0]["rain_total_mm"], 0.3
                    )
                    self.assertTrue(result["capabilities"]["dni"])
                outputs.append((result, queries))
            self.assertEqual(outputs[1], outputs[0])

    def test_fetch_errors_timeout_and_cancellation_remain_unhandled(self):
        for failure in ("error", "timeout", "cancel"):
            outcomes = []
            for cls in self.pair(timeout=0.002):
                calls = []

                async def fetch_all(sql, parameters):
                    calls.append((sql, parameters))
                    if failure == "error":
                        raise ValueError("owned database")
                    if failure == "cancel":
                        raise asyncio.CancelledError("owned cancellation")
                    await asyncio.sleep(1)

                provider = cls(SimpleNamespace(fetch_all=fetch_all))
                try:
                    asyncio.run(provider._fetch_all("owned query", (1,)))
                    self.fail("Expected the original unhandled failure")
                except (ValueError, TimeoutError, asyncio.CancelledError) as error:
                    outcomes.append((type(error).__name__, str(error), calls))
            self.assertEqual(outcomes[1], outcomes[0])
