# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""Read-only weather intelligence contract backed by the SFML database."""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MAX_HISTORY_DAYS = 731
_QUERY_TIMEOUT_SECONDS = 8


class SFMLWeatherIntelligenceProvider:
    """Expose sanitized station history and forecast comparison tracks."""

    contract_version = 1

    def __init__(self, data_manager: Any, time_zone: str | None = None) -> None:
        self._data_manager = data_manager
        try:
            self._time_zone = ZoneInfo(time_zone) if time_zone else timezone.utc
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            self._time_zone = timezone.utc
        return None if True else None

    async def snapshot(self, start: datetime, end: datetime) -> dict[str, Any]:
        end_utc = self._aware_utc(end)
        start_utc = max(
            self._aware_utc(start), end_utc - timedelta(days=_MAX_HISTORY_DAYS)
        )
        end_local = end_utc.astimezone(self._time_zone)
        start_local = start_utc.astimezone(self._time_zone)
        start_date = start_local.date().isoformat()
        end_date = end_local.date().isoformat()
        recent_start_date = (end_local - timedelta(hours=72)).date().isoformat()
        prediction_daily_rows = await self._fetch_all(
            "\n            SELECT substr(prediction_id, 1, 10) AS period,\n                   MIN(temperature) AS temperature_min,\n                   MAX(temperature) AS temperature_max,\n                   AVG(temperature) AS temperature_avg,\n                   COUNT(temperature) AS temperature_count,\n                   AVG(humidity) AS humidity_avg,\n                   AVG(pressure) AS pressure_avg,\n                   MAX(wind) AS wind_max,\n                   SUM(rain) AS rain_total,\n                   AVG(clouds) AS clouds_avg,\n                   AVG(solar_radiation_wm2) AS radiation_avg,\n                   MAX(solar_radiation_wm2) AS radiation_max,\n                   MAX(lux) AS lux_max\n            FROM prediction_weather\n            WHERE weather_type = 'actual'\n              AND prediction_id BETWEEN ? AND ?\n            GROUP BY substr(prediction_id, 1, 10)\n            ORDER BY period\n            ",
            (f"{start_date}_00", f"{end_date}_23"),
        )
        actual_daily_rows = await self._fetch_all(
            "\n            SELECT date AS period,\n                   MIN(temperature_c) AS temperature_min,\n                   MAX(temperature_c) AS temperature_max,\n                   AVG(temperature_c) AS temperature_avg,\n                   COUNT(temperature_c) AS temperature_count,\n                   AVG(humidity_percent) AS humidity_avg,\n                   AVG(pressure_hpa) AS pressure_avg,\n                   MAX(wind_speed_ms) * 3.6 AS wind_max,\n                   SUM(precipitation_mm) AS rain_total,\n                   AVG(cloud_cover_percent) AS clouds_avg,\n                   AVG(solar_radiation_wm2) AS radiation_avg,\n                   MAX(solar_radiation_wm2) AS radiation_max,\n                   MAX(lux) AS lux_max\n            FROM hourly_weather_actual\n            WHERE date BETWEEN ? AND ?\n            GROUP BY date\n            ORDER BY period\n            ",
            (recent_start_date, end_date),
        )
        actual_hourly_rows = await self._fetch_all(
            "\n            SELECT timestamp, date, hour, temperature_c, humidity_percent,\n                   pressure_hpa, wind_speed_ms, precipitation_mm,\n                   cloud_cover_percent, solar_radiation_wm2, lux, source\n            FROM hourly_weather_actual\n            WHERE date BETWEEN ? AND ?\n            ORDER BY timestamp\n            LIMIT 240\n            ",
            (recent_start_date, end_date),
        )
        blend_forecast_rows = await self._fetch_all(
            "\n            SELECT forecast_date, hour, temperature, humidity, pressure, wind,\n                   rain, clouds, cloud_cover_low, cloud_cover_mid,\n                   cloud_cover_high, solar_radiation_wm2, direct_radiation,\n                   diffuse_radiation, visibility_m, fog_detected, fog_type,\n                   weather_code, updated_at\n            FROM weather_forecast\n            WHERE forecast_date BETWEEN ? AND date(?, '+7 days')\n            ORDER BY forecast_date, hour\n            ",
            (end_date, end_date),
        )
        kepler_forecast_rows = await self._fetch_all(
            "\n            SELECT prediction_id, temperature, humidity, pressure, wind, rain,\n                   clouds, solar_radiation_wm2, direct_radiation,\n                   diffuse_radiation, source\n            FROM prediction_weather\n            WHERE weather_type = 'forecast'\n              AND prediction_id BETWEEN ? AND ?\n            ORDER BY prediction_id\n            ",
            (
                f"{end_date}_00",
                f"{(end_local + timedelta(days=7)).date().isoformat()}_23",
            ),
        )
        daily_by_date = {
            row["period"]: row
            for raw_row in prediction_daily_rows
            if (row := self._daily_row(raw_row))["period"]
        }
        for raw_row in actual_daily_rows:
            row = self._daily_row(raw_row)
            if row["period"]:
                daily_by_date[row["period"]] = row
        daily_rows = [daily_by_date[date_key] for date_key in sorted(daily_by_date)]
        actual_series = [
            row
            for raw_row in actual_hourly_rows
            if (row := self._hourly_actual_row(raw_row))["observed_at"] is not None
        ]
        blend_series = [
            row
            for raw_row in blend_forecast_rows
            if (row := self._blend_row(raw_row))["valid_at"] is not None
        ]
        kepler_series = [
            row
            for raw_row in kepler_forecast_rows
            if (row := self._kepler_row(raw_row))["valid_at"] is not None
        ]
        precipitation = self._precipitation(daily_rows)
        return {
            "contract_version": self.contract_version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "available": bool(daily_rows or actual_series),
            "status": "available" if daily_rows or actual_series else "unavailable",
            "source": "sfml_database",
            "aggregates": {
                "status": "ok" if daily_rows else "unavailable",
                "source": "sfml_database",
                "data_from": daily_rows[0]["period"] if daily_rows else start_date,
                "data_to": daily_rows[-1]["period"] if daily_rows else end_date,
                "coverage": {"observations": len(daily_rows)},
                "data": daily_rows,
            },
            "summary": self._summary(daily_rows, start_local, end_local),
            "series": {
                "status": "ok" if actual_series else "unavailable",
                "source": "sfml_database",
                "data": actual_series,
            },
            "recent": {
                "status": "ok" if actual_series else "unavailable",
                "source": "sfml_database",
                "data": actual_series,
            },
            "record_events": self._records(daily_rows),
            "monthly_series": self._monthly(daily_rows),
            "precipitation": precipitation,
            "year_comparison": self._year_comparison(daily_rows, end_date),
            "source_comparison": {
                "sfml_expert_blend": blend_series,
                "kepler_input": kepler_series,
            },
            "capabilities": {
                "station_actuals": True,
                "rain": any((row["rain_total_mm"] is not None for row in daily_rows)),
                "ghi": any(
                    (row["radiation_avg_wm2"] is not None for row in daily_rows)
                ),
                "dni": any(
                    (
                        self._value(raw_row, "direct_radiation") is not None
                        for raw_row in blend_forecast_rows
                    )
                ),
                "dhi": any(
                    (
                        self._value(raw_row, "diffuse_radiation") is not None
                        for raw_row in blend_forecast_rows
                    )
                ),
                "lux": any((row["lux_max"] is not None for row in daily_rows)),
            },
        }

    async def _fetch_all(self, sql: str, parameters: tuple[Any, ...]) -> list[Any]:
        return await asyncio.wait_for(
            self._data_manager.fetch_all(sql, parameters),
            timeout=_QUERY_TIMEOUT_SECONDS,
        )

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _value(row: Any, key: str) -> Any:
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return

    @classmethod
    def _daily_row(cls, row: Any) -> dict[str, Any]:

        def number(key: str) -> float | None:
            numeric_value = cls._value(row, key)
            return (
                round(float(numeric_value), 3)
                if isinstance(numeric_value, (int, float))
                else None
            )

        return {
            "period": str(cls._value(row, "period")),
            "values": {
                "temperature": {
                    "min": number("temperature_min"),
                    "max": number("temperature_max"),
                    "avg": number("temperature_avg"),
                    "count": int(cls._value(row, "temperature_count") or 0),
                },
                "humidity": {"avg": number("humidity_avg")},
                "pressure": {"avg": number("pressure_avg")},
                "wind_speed": {"max": number("wind_max")},
                "precipitation": {"amount": number("rain_total")},
                "cloud_cover": {"avg": number("clouds_avg")},
                "solar_radiation": {
                    "avg": number("radiation_avg"),
                    "max": number("radiation_max"),
                },
                "lux": {"max": number("lux_max")},
            },
            "rain_total_mm": number("rain_total"),
            "radiation_avg_wm2": number("radiation_avg"),
            "radiation_max_wm2": number("radiation_max"),
            "lux_max": number("lux_max"),
        }

    def _actual_row(self, row: Any) -> dict[str, Any]:
        prediction_id = str(self._value(row, "prediction_id") or "")
        observed_at = (
            self._utc_at(prediction_id[:10], prediction_id[11:13])
            if len(prediction_id) >= 13
            else None
        )
        field_mapping = {
            "temperature": "temperature",
            "humidity": "humidity",
            "pressure": "pressure",
            "wind": "wind_speed",
            "rain": "precipitation",
            "clouds": "cloud_cover",
            "solar_radiation_wm2": "solar_radiation_wm2",
            "lux": "lux",
        }
        values = {
            target_field: float(numeric_value)
            for source_field, target_field in field_mapping.items()
            if isinstance(
                (numeric_value := self._value(row, source_field)), (int, float)
            )
        }
        return {"observed_at": observed_at, "period": observed_at, "values": values}

    def _hourly_actual_row(self, row: Any) -> dict[str, Any]:
        observed_at = self._utc_timestamp(self._value(row, "timestamp"))
        field_mapping = {
            "temperature_c": "temperature",
            "humidity_percent": "humidity",
            "pressure_hpa": "pressure",
            "precipitation_mm": "precipitation",
            "cloud_cover_percent": "cloud_cover",
            "solar_radiation_wm2": "solar_radiation_wm2",
            "lux": "lux",
        }
        values = {
            target_field: float(numeric_value)
            for source_field, target_field in field_mapping.items()
            if isinstance(
                (numeric_value := self._value(row, source_field)), (int, float)
            )
        }
        wind_speed_ms = self._value(row, "wind_speed_ms")
        if isinstance(wind_speed_ms, (int, float)):
            values["wind_speed"] = round(float(wind_speed_ms) * 3.6, 3)
        return {
            "observed_at": observed_at,
            "period": observed_at,
            "values": values,
            "source": self._value(row, "source"),
        }

    def _blend_row(self, row: Any) -> dict[str, Any]:
        return {
            "valid_at": self._utc_at(
                str(self._value(row, "forecast_date")),
                int(self._value(row, "hour") or 0),
            ),
            "source": "sfml_expert_blend",
            **{
                mapped_field: self._value(row, source_field)
                for source_field, mapped_field in {
                    "temperature": "temperature_c",
                    "humidity": "humidity",
                    "pressure": "pressure",
                    "wind": "wind_speed",
                    "rain": "precipitation",
                    "clouds": "cloud_cover",
                    "cloud_cover_low": "cloud_cover_low",
                    "cloud_cover_mid": "cloud_cover_mid",
                    "cloud_cover_high": "cloud_cover_high",
                    "solar_radiation_wm2": "ghi_wm2",
                    "direct_radiation": "dni_wm2",
                    "diffuse_radiation": "dhi_wm2",
                    "visibility_m": "visibility_m",
                    "fog_detected": "fog_detected",
                    "fog_type": "fog_type",
                    "weather_code": "weather_code",
                    "updated_at": "issued_at",
                }.items()
            },
        }

    def _kepler_row(self, row: Any) -> dict[str, Any]:
        prediction_id = str(self._value(row, "prediction_id") or "")
        return {
            "valid_at": self._utc_at(prediction_id[:10], prediction_id[11:13]),
            "source": "kepler_input",
            "provider": self._value(row, "source"),
            **{
                mapped_field: self._value(row, source_field)
                for source_field, mapped_field in {
                    "temperature": "temperature_c",
                    "humidity": "humidity",
                    "pressure": "pressure",
                    "wind": "wind_speed",
                    "rain": "precipitation",
                    "clouds": "cloud_cover",
                    "solar_radiation_wm2": "ghi_wm2",
                    "direct_radiation": "dni_wm2",
                    "diffuse_radiation": "dhi_wm2",
                }.items()
            },
        }

    def _utc_at(self, date_value: str, hour_value: str | int) -> str | None:
        naive_datetime = datetime.fromisoformat(
            f"{date_value}T{int(hour_value):02d}:00:00"
        )
        local_datetime = naive_datetime.replace(tzinfo=self._time_zone, fold=0)
        utc_datetime = local_datetime.astimezone(timezone.utc)
        if (
            utc_datetime.astimezone(self._time_zone).replace(tzinfo=None)
            != naive_datetime
        ):
            return
        return utc_datetime.isoformat()

    def _utc_timestamp(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return
        try:
            parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return
        if parsed_datetime.tzinfo is None:
            parsed_datetime = parsed_datetime.replace(tzinfo=self._time_zone)
        return parsed_datetime.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _summary(
        daily: list[dict[str, Any]], start: datetime, end: datetime
    ) -> dict[str, Any]:
        temperature_rows = [row["values"]["temperature"] for row in daily]
        minimum_temperatures = [
            row["min"] for row in temperature_rows if row["min"] is not None
        ]
        maximum_temperatures = [
            row["max"] for row in temperature_rows if row["max"] is not None
        ]
        weighted_temperatures = [
            (row["avg"], row["count"])
            for row in temperature_rows
            if row["avg"] is not None
            if row["count"]
        ]
        temperature_count = sum((row[1] for row in weighted_temperatures))
        radiation_averages = [
            row["radiation_avg_wm2"]
            for row in daily
            if row["radiation_avg_wm2"] is not None
        ]
        radiation_maxima = [
            row["radiation_max_wm2"]
            for row in daily
            if row["radiation_max_wm2"] is not None
        ]
        return (
            (
                (
                    {
                        "data_since": daily[0]["period"] if daily else None,
                        "data_to": daily[-1]["period"] if daily else None,
                        "valid_days": len(daily),
                        "coverage_percent": round(
                            min(
                                100,
                                len(daily)
                                / max(1, (end.date() - start.date()).days + 1)
                                * 100,
                            ),
                            1,
                        ),
                        "temperature_min_c": (
                            min(minimum_temperatures) if minimum_temperatures else None
                        ),
                        "temperature_max_c": (
                            max(maximum_temperatures) if maximum_temperatures else None
                        ),
                        "temperature_avg_c": (
                            round(
                                sum(
                                    (
                                        average * count
                                        for average, count in weighted_temperatures
                                    )
                                )
                                / temperature_count,
                                2,
                            )
                            if temperature_count
                            else None
                        ),
                        "solar_radiation_avg_wm2": (
                            round(sum(radiation_averages) / len(radiation_averages), 2)
                            if radiation_averages
                            else None
                        ),
                        "solar_radiation_max_wm2": (
                            max(radiation_maxima) if radiation_maxima else None
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
            if True
            else None
        )

    @staticmethod
    def _precipitation(daily: list[dict[str, Any]]) -> dict[str, Any]:
        rain_rows = [
            {"date": row["period"], "total_mm": row["rain_total_mm"]}
            for row in daily
            if row["rain_total_mm"] is not None
        ]
        longest_dry_spell = current_dry_spell = 0
        for row in rain_rows:
            current_dry_spell = current_dry_spell + 1 if row["total_mm"] < 0.2 else 0
            longest_dry_spell = max(longest_dry_spell, current_dry_spell)
        return {
            "available": bool(rain_rows),
            "source": "sfml_database",
            "total_mm": (
                round(sum((row["total_mm"] for row in rain_rows)), 3)
                if rain_rows
                else None
            ),
            "daily": rain_rows,
            "rain_days": sum((row["total_mm"] >= 0.2 for row in rain_rows)),
            "dry_spell_days": longest_dry_spell,
        }

    @staticmethod
    def _records(daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
        temperature_records = [
            (row["values"]["temperature"], row["period"])
            for row in daily
            if row["values"]["temperature"]["min"] is not None
        ]
        if not temperature_records:
            return []
        coldest = min(temperature_records, key=lambda item: item[0]["min"])
        hottest = max(temperature_records, key=lambda item: item[0]["max"])
        return [
            {
                "type": "coldest_temperature",
                "value": coldest[0]["min"],
                "unit": "°C",
                "date": coldest[1],
            },
            {
                "type": "hottest_temperature",
                "value": hottest[0]["max"],
                "unit": "°C",
                "date": hottest[1],
            },
        ]

    @staticmethod
    def _monthly(daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
        daily_by_month = {}
        for row in daily:
            daily_by_month.setdefault(row["period"][:7], []).append(row)
        monthly_rows = []
        for month, year_rows in daily_by_month.items():
            temperature_rows = [
                raw_row["values"]["temperature"] for raw_row in year_rows
            ]
            minimum_temperatures = [
                row["min"] for row in temperature_rows if row["min"] is not None
            ]
            maximum_temperatures = [
                row["max"] for row in temperature_rows if row["max"] is not None
            ]
            average_temperatures = [
                row["avg"] for row in temperature_rows if row["avg"] is not None
            ]
            monthly_rows.append(
                {
                    "month": month,
                    "temperature_min_c": (
                        min(minimum_temperatures) if minimum_temperatures else None
                    ),
                    "temperature_max_c": (
                        max(maximum_temperatures) if maximum_temperatures else None
                    ),
                    "temperature_avg_c": (
                        round(sum(average_temperatures) / len(average_temperatures), 2)
                        if average_temperatures
                        else None
                    ),
                }
            )
        return monthly_rows

    @staticmethod
    def _year_comparison(
        daily: list[dict[str, Any]], current_date: str
    ) -> dict[str, Any]:
        current_year = int(current_date[:4])

        def summarize(year: int) -> dict[str, Any]:
            year_rows = [row for row in daily if row["period"].startswith(f"{year}-")]
            temperature_rows = [row["values"]["temperature"] for row in year_rows]
            weighted_temperatures = [
                (row["avg"], row["count"])
                for row in temperature_rows
                if row["avg"] is not None
                if row["count"]
            ]
            temperature_count = sum((row[1] for row in weighted_temperatures))
            rain_totals = [
                row["rain_total_mm"]
                for row in year_rows
                if row["rain_total_mm"] is not None
            ]
            longest_dry_spell = current_dry_spell = 0
            for rain_total in rain_totals:
                current_dry_spell = current_dry_spell + 1 if rain_total < 0.2 else 0
                longest_dry_spell = max(longest_dry_spell, current_dry_spell)
            return (
                (
                    (
                        {
                            "year": year,
                            "valid_days": len(year_rows),
                            "temperature_avg_c": (
                                round(
                                    sum(
                                        (
                                            average * count
                                            for average, count in weighted_temperatures
                                        )
                                    )
                                    / temperature_count,
                                    2,
                                )
                                if temperature_count
                                else None
                            ),
                            "precipitation_total_mm": (
                                round(sum(rain_totals), 3) if rain_totals else None
                            ),
                            "rain_days": (
                                sum((rain_total >= 0.2 for rain_total in rain_totals))
                                if rain_totals
                                else None
                            ),
                            "dry_spell_days": (
                                longest_dry_spell if rain_totals else None
                            ),
                        }
                        if True
                        else None
                    )
                    if True
                    else None
                )
                if True
                else None
            )

        try:
            previous_date = (
                datetime.fromisoformat(current_date)
                .date()
                .replace(year=current_year - 1)
                .isoformat()
            )
        except ValueError:
            previous_date = f"{current_year - 1}-02-28"
        previous_same_day = next(
            (row for row in daily if row["period"] == previous_date), None
        )
        return (
            (
                {
                    "available": bool(daily),
                    "current": summarize(current_year),
                    "previous": summarize(current_year - 1),
                    "same_day_last_year": {
                        "date": previous_date,
                        "temperature_avg_c": (
                            previous_same_day["values"]["temperature"]["avg"]
                            if previous_same_day
                            else None
                        ),
                    },
                }
                if True
                else None
            )
            if True
            else None
        )
