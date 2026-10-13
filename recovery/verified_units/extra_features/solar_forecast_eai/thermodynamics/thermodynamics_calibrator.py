# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Thermodynamics Calibrator Module.
Self-learning calibrator for heat pump consumption forecasting using database storage.

Learns building-specific correction factors from actual consumption vs predicted.
Uses temperature-based buckets instead of solar weather buckets.

@zara
"""
from __future__ import annotations
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from statistics import median
from typing import Any, Dict, Optional
from ..data.db_manager import DatabaseManager

_LOGGER = logging.getLogger(__name__)
MIN_CONSUMPTION_KWH = 0.05
MAX_CORRECTION_FACTOR = 2.5
MIN_CORRECTION_FACTOR = 0.4
MILD_CORRECTION_FLOOR = 0.08
ROLLING_WINDOW_DAYS = 21
DAILY_RATIO_MIN_DAYS = 5
DAILY_RATIO_REJECT_BELOW = 0.02
DAILY_RATIO_REJECT_ABOVE = 10.0
DAILY_RATIOS_STATE_KEY = "daily_ratios_json"
DAILY_RATIO_MIGRATION_KEY = "daily_ratio_migration_v1"
MIN_SAMPLES_FOR_FACTOR = 1
MIN_SAMPLES_FOR_FULL_CONFIDENCE = 7
U_FACTOR_ADOPT_SAMPLES = 24
U_FACTOR_REALIGN_REL = 0.05
ABOVE_LIMIT_DHW_FACTOR = 1.3
ABOVE_LIMIT_MIN_DAYS = 7
ABOVE_LIMIT_MEAN_KWH_H = 0.05
LIMIT_REVOKE_DAYS = 7
EMA_ALPHA = 0.15
EMA_MIN_SAMPLES_FOR_EMA = 3


def selectable_future_hours(rows: list, locked_dates: set[str], today: str) -> list:
    """Future hours whose forecast date is not locked."""
    selectable_rows = []
    for row in rows:
        try:
            forecast_date = str(row[1])
        except (IndexError, TypeError):
            continue
        if forecast_date < today or forecast_date in locked_dates:
            continue
        selectable_rows.append(row)
    return selectable_rows


class TemperatureBucket(Enum):
    """Temperature-based condition buckets for calibration. @zara

    Replaces solar WeatherBucket (CLEAR/CLOUDY/RAINY) with
    temperature ranges that are relevant for heat pump efficiency.
    """

    MILD = "mild"
    MILD_WINDY = "mild_windy"
    MODERATE = "moderate"
    MODERATE_WINDY = "moderate_windy"
    COLD = "cold"
    COLD_WINDY = "cold_windy"
    FROST = "frost"
    FROST_WINDY = "frost_windy"
    EXTREME_COLD = "extreme_cold"
    EXTREME_COLD_WINDY = "extreme_cold_windy"
    DHW_ONLY = "dhw_only"


BUCKET_MILD_MIN = 10.0
BUCKET_MODERATE_MIN = 5.0
BUCKET_COLD_MIN = 0.0
BUCKET_FROST_MIN = -5.0
WINDY_THRESHOLD = 5.0


@dataclass
class BucketFactors:
    """Calibration factors for a specific temperature bucket. @zara"""

    global_factor: float = 1.0
    hourly_factors: Dict[int, float] = field(default_factory=dict)
    sample_count: int = 0
    confidence: float = 0.0


@dataclass
class CalibrationResult:
    """Result of a calibration calculation. @zara"""

    bucket: TemperatureBucket
    correction_factor: float
    confidence: float
    sample_count: int
    u_factor_estimate: float


@dataclass
class CalibrationEntry:
    """Single calibration data point. @zara"""

    timestamp: datetime
    bucket: TemperatureBucket
    predicted_kwh: float
    actual_kwh: float
    outdoor_temp: float
    indoor_temp: float
    correction_factor: float


class ThermodynamicsCalibrator:
    """Self-learning calibrator for heat pump consumption predictions. @zara

    Learns building-specific correction factors by comparing actual consumption
    against predicted consumption. Uses temperature-based buckets for
    condition-specific calibration.

    The calibrator also estimates the building's U-factor (heat loss coefficient)
    from the observed data, which is fed back to the ThermodynamicsEngine.
    """

    def __init__(self, db_manager: DatabaseManager) -> None:
        """Initialize calibrator. @zara"""
        self._db = db_manager
        self._bucket_factors = {
            temperature_bucket: BucketFactors()
            for temperature_bucket in TemperatureBucket
        }
        self._u_factor_estimate = 0.4
        self._u_factor_samples = 0
        self._u_factor_aligned = None
        self._bucket_conditions = {}
        self._hour_split = {}
        self._above_limit_days = 0
        self._above_limit_mean_kwh_h = 0.0
        self._limit_lifted = False
        self._limit_clear_days = 0
        self._initialized = False
        self._daily_ratios = {
            temperature_bucket: [] for temperature_bucket in TemperatureBucket
        }
        self._daily_factor_active = {
            temperature_bucket: False for temperature_bucket in TemperatureBucket
        }
        self._drift_days = []

    @property
    def u_factor_estimate(self) -> float:
        """Current estimated building heat loss factor. @zara"""
        return self._u_factor_estimate

    @property
    def u_factor_samples(self) -> int:
        """Accepted building heat-loss samples, including the EMA branch."""
        return self._u_factor_samples

    @property
    def u_factor_aligned(self) -> Optional[float]:
        """U-factor the heating bucket factors are aligned to, if adopted."""
        return self._u_factor_aligned

    @property
    def above_limit_days(self) -> int:
        """Suppressed days recorded for a plant without hot water."""
        return self._above_limit_days

    @property
    def above_limit_mean_kwh_h(self) -> float:
        """Mean measured kWh/h on those suppressed days."""
        return self._above_limit_mean_kwh_h

    def heating_above_limit_proven(self, has_dhw: bool) -> bool:
        """True when observations show space heating above the temperature limit.

        With hot water, the DHW-only factor must exceed 1.3 at full confidence.
        Without hot water, at least seven suppressed days must average more
        than 0.05 kWh/h.
        """
        if has_dhw:
            dhw_calibration = self.get_correction_factor(
                0.0, 0.0, hour=-1, is_dhw_only=True
            )
            return (
                float(dhw_calibration.confidence) >= 1.0
                and float(dhw_calibration.correction_factor) > ABOVE_LIMIT_DHW_FACTOR
            )
        return (
            self._above_limit_days >= ABOVE_LIMIT_MIN_DAYS
            and self._above_limit_mean_kwh_h > ABOVE_LIMIT_MEAN_KWH_H
        )

    def heating_limit_is_lifted(self, has_dhw: bool) -> bool:
        """Whether forecasts should ignore the temperature limit.

        Evidence lifts immediately. Clearing the evidence does not restore the
        limit until seven consecutive suppressible days fail to renew it.
        """
        if self._limit_clear_days >= LIMIT_REVOKE_DAYS:
            return False
        if self.heating_above_limit_proven(has_dhw):
            return True
        return self._limit_lifted

    async def note_suppressible_day(
        self, has_dhw: bool, day_mean_kwh_h: float | None = None
    ) -> None:
        """Update the lift latch after one day the temperature limit could apply."""
        if has_dhw:
            above_limit_proven = self.heating_above_limit_proven(True)
        else:
            try:
                day_mean = None if day_mean_kwh_h is None else float(day_mean_kwh_h)
            except (TypeError, ValueError):
                day_mean = None
            if day_mean is None or not math.isfinite(day_mean):
                above_limit_proven = self.heating_above_limit_proven(False)
            else:
                above_limit_proven = (
                    day_mean > ABOVE_LIMIT_MEAN_KWH_H
                    and self._above_limit_days >= ABOVE_LIMIT_MIN_DAYS
                )
        if above_limit_proven:
            self._limit_clear_days = 0
            self._limit_lifted = True
        else:
            self._limit_clear_days += 1
            if self._limit_clear_days >= LIMIT_REVOKE_DAYS:
                self._limit_lifted = False
        await self._persist_limit_latch()

    async def _persist_limit_latch(self) -> None:
        """Store the lift latch in the existing thermodynamics_state table."""
        try:
            await self._db.execute(
                "INSERT OR REPLACE INTO thermodynamics_state (key, value) VALUES (?, ?)",
                ("heating_limit_lifted", "1" if self._limit_lifted else "0"),
            )
            await self._db.execute(
                "INSERT OR REPLACE INTO thermodynamics_state (key, value) VALUES (?, ?)",
                ("heating_limit_clear_days", str(self._limit_clear_days)),
            )
        except Exception as error:
            _LOGGER.warning("Failed to persist heating-limit latch: %s", error)
        return None if True else None

    async def record_above_limit_day(self, mean_kwh_h: float) -> None:
        """Fold one suppressed day's mean kWh/h into the no-hot-water counter."""
        try:
            mean_consumption = float(mean_kwh_h)
        except (TypeError, ValueError):
            return
        if not math.isfinite(mean_consumption) or mean_consumption < 0.0:
            return None
        previous_days = self._above_limit_days
        self._above_limit_mean_kwh_h = (
            self._above_limit_mean_kwh_h * previous_days + mean_consumption
        ) / (previous_days + 1)
        self._above_limit_days = previous_days + 1
        await self._persist_above_limit()

    async def _persist_above_limit(self) -> None:
        """Store the no-hot-water above-limit counter in thermodynamics_state."""
        try:
            await self._db.execute(
                "INSERT OR REPLACE INTO thermodynamics_state (key, value) VALUES (?, ?)",
                ("above_limit_day_count", str(self._above_limit_days)),
            )
            await self._db.execute(
                "INSERT OR REPLACE INTO thermodynamics_state (key, value) VALUES (?, ?)",
                ("above_limit_mean_kwh_h", str(self._above_limit_mean_kwh_h)),
            )
        except Exception as error:
            _LOGGER.warning("Failed to persist above-limit heating counter: %s", error)
        return None if True else None

    @property
    def is_initialized(self) -> bool:
        """Whether calibrator has loaded state from DB. @zara"""
        return self._initialized

    @classmethod
    def classify_bucket(
        cls, outdoor_temp: float, wind_speed: float = 0.0, is_dhw_only: bool = False
    ) -> TemperatureBucket:
        """Classify conditions into a temperature bucket. @zara"""
        if is_dhw_only:
            return TemperatureBucket.DHW_ONLY
        windy = wind_speed >= WINDY_THRESHOLD
        if outdoor_temp >= BUCKET_MILD_MIN:
            return TemperatureBucket.MILD_WINDY if windy else TemperatureBucket.MILD
        if outdoor_temp >= BUCKET_MODERATE_MIN:
            return (
                TemperatureBucket.MODERATE_WINDY
                if windy
                else TemperatureBucket.MODERATE
            )
        if outdoor_temp >= BUCKET_COLD_MIN:
            return TemperatureBucket.COLD_WINDY if windy else TemperatureBucket.COLD
        if outdoor_temp >= BUCKET_FROST_MIN:
            return TemperatureBucket.FROST_WINDY if windy else TemperatureBucket.FROST
        return (
            TemperatureBucket.EXTREME_COLD_WINDY
            if windy
            else TemperatureBucket.EXTREME_COLD
        )

    def get_correction_factor(
        self,
        outdoor_temp: float,
        wind_speed: float = 0.0,
        hour: int = -1,
        is_dhw_only: bool = False,
    ) -> CalibrationResult:
        """Get correction factor for given conditions. @zara"""
        temperature_bucket = self.classify_bucket(outdoor_temp, wind_speed, is_dhw_only)
        settled_factor = self._settled_daily_factor(temperature_bucket)
        if settled_factor is not None:
            correction_factor, sample_count = settled_factor
            return CalibrationResult(
                bucket=temperature_bucket,
                correction_factor=correction_factor,
                confidence=1.0,
                sample_count=sample_count,
                u_factor_estimate=self._u_factor_estimate,
            )
        bucket_factors = self._bucket_factors.get(temperature_bucket, BucketFactors())
        if hour >= 0 and hour in bucket_factors.hourly_factors:
            correction_factor = bucket_factors.hourly_factors[hour]
        else:
            correction_factor = bucket_factors.global_factor
        return CalibrationResult(
            bucket=temperature_bucket,
            correction_factor=correction_factor,
            confidence=bucket_factors.confidence,
            sample_count=bucket_factors.sample_count,
            u_factor_estimate=self._u_factor_estimate,
        )

    def settled_day_factor(
        self, outdoor_temp: float, wind_speed: float = 0.0
    ) -> Optional[float]:
        """Factor for a whole day, or None while that mean-temperature bucket is hourly."""
        temperature_bucket = self.classify_bucket(outdoor_temp, wind_speed, False)
        settled_factor = self._settled_daily_factor(temperature_bucket)
        if settled_factor is None:
            return
        return settled_factor[0]

    def _settled_daily_factor(
        self, bucket: TemperatureBucket
    ) -> Optional[tuple[float, int]]:
        """Active day-level factor, or None while the bucket is still hourly."""
        if bucket == TemperatureBucket.DHW_ONLY:
            return
        if not self._daily_factor_active.get(bucket):
            return
        daily_ratios = self._daily_ratios.get(bucket) or []
        sample_count = len(daily_ratios)
        if sample_count >= DAILY_RATIO_MIN_DAYS:
            correction_factor = self._clamp_bucket(
                bucket,
                float(median((day_record["ratio"] for day_record in daily_ratios))),
            )
        else:
            bucket_factors = self._bucket_factors.get(bucket, BucketFactors())
            correction_factor = self._clamp_bucket(bucket, bucket_factors.global_factor)
            sample_count = max(sample_count, bucket_factors.sample_count)
        return (correction_factor, max(sample_count, DAILY_RATIO_MIN_DAYS))

    @staticmethod
    def _bucket_floor(bucket: TemperatureBucket) -> float:
        if bucket in {
            TemperatureBucket.MILD,
            TemperatureBucket.MILD_WINDY,
            TemperatureBucket.MODERATE,
            TemperatureBucket.MODERATE_WINDY,
        }:
            return MILD_CORRECTION_FLOOR
        return MIN_CORRECTION_FACTOR

    def _clamp_bucket(self, bucket: TemperatureBucket, value: float) -> float:
        return max(self._bucket_floor(bucket), min(float(value), MAX_CORRECTION_FACTOR))

    async def record_daily_ratio(
        self,
        *,
        day: date,
        actual_kwh_by_hour: list[Any],
        physics_kwh_by_hour: list[Any],
        outdoor_c_by_hour: list[Any],
        wind_m_s_by_hour: Optional[list[Any]] = None,
        heating_capacity_kw: float,
        persist: bool = True,
    ) -> str:
        """Learn one local day, or keep the stored factor.

        The day is the midnight-to-midnight total. A missing hour, including
        the midnight hour, rejects the day instead of summing the holes.
        Physics is the forecast physics at the U already adopted for that day.
        """
        parsed_hours = self._parse_day_hours(
            actual_kwh_by_hour, physics_kwh_by_hour, outdoor_c_by_hour, wind_m_s_by_hour
        )
        if parsed_hours is None:
            return "hole"
        actual_kwh, physics_kwh, mean_outdoor_c, mean_wind_m_s = parsed_hours
        try:
            capacity_kw = float(heating_capacity_kw)
        except (TypeError, ValueError):
            capacity_kw = 0.0
        if not math.isfinite(capacity_kw) or capacity_kw <= 0.0:
            return "no_capacity"
        if actual_kwh > capacity_kw * 24.0:
            return "over_cap"
        if physics_kwh <= 0.0:
            return "no_physics"
        ratio = actual_kwh / physics_kwh
        if ratio < DAILY_RATIO_REJECT_BELOW or ratio > DAILY_RATIO_REJECT_ABOVE:
            return "bad_ratio"
        calibration = self.get_correction_factor(mean_outdoor_c, mean_wind_m_s)
        drift_correction = (
            calibration.correction_factor
            if calibration.confidence > 0.3 and calibration.correction_factor > 0.0
            else 1.0
        )
        self.note_drift_day(day, physics_kwh * drift_correction, actual_kwh)
        temperature_bucket = self.classify_bucket(mean_outdoor_c, mean_wind_m_s, False)
        retained_days = [
            day_record
            for day_record in self._daily_ratios.get(temperature_bucket, [])
            if day_record["day"] != day.isoformat()
            if day_record["day"]
            >= (day - timedelta(days=ROLLING_WINDOW_DAYS - 1)).isoformat()
        ]
        retained_days.append({"day": day.isoformat(), "ratio": ratio})
        retained_days.sort(key=lambda item: item["day"])
        self._daily_ratios[temperature_bucket] = retained_days
        if len(retained_days) >= DAILY_RATIO_MIN_DAYS:
            correction_factor = self._clamp_bucket(
                temperature_bucket,
                float(median((day_record["ratio"] for day_record in retained_days))),
            )
            bucket_factors = self._bucket_factors.setdefault(
                temperature_bucket, BucketFactors()
            )
            bucket_factors.global_factor = correction_factor
            bucket_factors.sample_count = len(retained_days)
            bucket_factors.confidence = 1.0
            self._daily_factor_active[temperature_bucket] = True
            if persist:
                await self._save_bucket_factors(temperature_bucket, bucket_factors)
                await self._persist_daily_ratios()
            return "recorded"
        if persist:
            await self._persist_daily_ratios()
        return "kept"

    @staticmethod
    def _parse_day_hours(
        actual_kwh_by_hour: list[Any],
        physics_kwh_by_hour: list[Any],
        outdoor_c_by_hour: list[Any],
        wind_m_s_by_hour: Optional[list[Any]],
    ) -> Optional[tuple[float, float, float, float]]:
        if (
            len(actual_kwh_by_hour) != 24
            or len(physics_kwh_by_hour) != 24
            or len(outdoor_c_by_hour) != 24
        ):
            return None
        wind_values = wind_m_s_by_hour if wind_m_s_by_hour is not None else [0.0] * 24
        if len(wind_values) != 24:
            return
        actual_total = 0.0
        physics_total = 0.0
        outdoor_total = 0.0
        wind_total = 0.0
        for hour_index in range(24):
            try:
                actual_kwh = float(actual_kwh_by_hour[hour_index])
                physics_kwh = float(physics_kwh_by_hour[hour_index])
                outdoor_c = float(outdoor_c_by_hour[hour_index])
                wind_m_s = float(wind_values[hour_index])
            except (TypeError, ValueError):
                return
            if not all(
                (
                    math.isfinite(value)
                    for value in (actual_kwh, physics_kwh, outdoor_c, wind_m_s)
                )
            ):
                return
            if actual_kwh < 0.0 or physics_kwh < 0.0:
                return
            actual_total += actual_kwh
            physics_total += physics_kwh
            outdoor_total += outdoor_c
            wind_total += wind_m_s
        return (actual_total, physics_total, outdoor_total / 24.0, wind_total / 24.0)

    def note_drift_day(self, day: date, forecast_kwh: float, actual_kwh: float) -> None:
        """Remember one completed day's forecast/actual ratio."""
        try:
            forecast_energy = float(forecast_kwh)
            actual_energy = float(actual_kwh)
        except (TypeError, ValueError):
            return
        if (
            not math.isfinite(forecast_energy)
            or not math.isfinite(actual_energy)
            or actual_energy <= 0.0
            or (forecast_energy < 0.0)
        ):
            return None
        day_key = day.isoformat()
        self._drift_days = [
            day_record
            for day_record in self._drift_days
            if day_record["day"] != day_key
        ]
        self._drift_days.append(
            {"day": day_key, "ratio": forecast_energy / actual_energy}
        )
        self._drift_days.sort(key=lambda item: item["day"])
        self._drift_days = self._drift_days[-ROLLING_WINDOW_DAYS:]

    def drift_status(self, today: Optional[date] = None) -> str:
        """Seven-day median of forecast/actual, excluding today."""
        today_key = today.isoformat() if isinstance(today, date) else None
        recent_ratios = [
            float(day_record["ratio"])
            for day_record in self._drift_days
            if day_record["day"] != today_key
            if math.isfinite(float(day_record["ratio"]))
        ]
        recent_ratios = recent_ratios[-7:]
        if len(recent_ratios) < 7:
            return "insufficient_history"
        median_ratio = float(median(recent_ratios))
        if median_ratio < 0.5 or median_ratio > 2.0:
            return "degraded"
        return "stable"

    async def migration_already_done(self) -> bool:
        migration_state_row = await self._db.fetchone(
            "SELECT value FROM thermodynamics_state WHERE key = 'daily_ratio_migration_v1'"
        )
        return bool(migration_state_row and str(migration_state_row[0]).strip() == "1")

    async def run_history_migration(
        self, rows: list[Any], *, heating_capacity_kw: float
    ) -> dict[str, Any]:
        """Relearn day ratios once from stored hours. Too little history changes nothing."""
        import time as time_module

        migration_started = time_module.perf_counter()
        if await self.migration_already_done():
            return {
                "ran": False,
                "reason": "already",
                "elapsed_s": time_module.perf_counter() - migration_started,
            }
        previous_daily_ratios = {
            temperature_bucket: [dict(day_record) for day_record in bucket_days]
            for temperature_bucket, bucket_days in self._daily_ratios.items()
        }
        previous_daily_activity = dict(self._daily_factor_active)
        previous_bucket_factors = {
            temperature_bucket: BucketFactors(
                global_factor=bucket_factors.global_factor,
                hourly_factors=dict(bucket_factors.hourly_factors),
                sample_count=bucket_factors.sample_count,
                confidence=bucket_factors.confidence,
            )
            for temperature_bucket, bucket_factors in self._bucket_factors.items()
        }
        hours_by_date = {}
        for row in rows:
            parsed_row = self._migration_row(row)
            if not parsed_row is not None:
                continue
            (
                day_key,
                hour_index,
                actual_kwh,
                physics_kwh,
                outdoor_c,
                wind_m_s,
                excluded,
            ) = parsed_row
            if excluded:
                hours_by_date.setdefault(day_key, {})
                hours_by_date[day_key][hour_index] = None
                continue
            hours_by_date.setdefault(day_key, {})[hour_index] = (
                actual_kwh,
                physics_kwh,
                outdoor_c,
                wind_m_s,
            )
        for day_key, day_hours in hours_by_date.items():
            try:
                parsed_day = date.fromisoformat(day_key)
            except ValueError:
                continue
            actual_values = []
            physics_kwh = []
            outdoor_values = []
            wind_values = []
            for hour_index in range(24):
                hour_values = day_hours.get(hour_index)
                if hour_values is None:
                    actual_values.append(None)
                    physics_kwh.append(None)
                    outdoor_values.append(None)
                    wind_values.append(None)
                else:
                    actual_values.append(hour_values[0])
                    physics_kwh.append(hour_values[1])
                    outdoor_values.append(hour_values[2])
                    wind_values.append(hour_values[3])
            await self.record_daily_ratio(
                day=parsed_day,
                actual_kwh_by_hour=actual_values,
                physics_kwh_by_hour=physics_kwh,
                outdoor_c_by_hour=outdoor_values,
                wind_m_s_by_hour=wind_values,
                heating_capacity_kw=heating_capacity_kw,
                persist=False,
            )
            continue
        if not any(self._daily_factor_active.values()):
            self._daily_ratios = previous_daily_ratios
            self._daily_factor_active = previous_daily_activity
            self._bucket_factors = previous_bucket_factors
            return {
                "ran": False,
                "reason": "insufficient_history",
                "elapsed_s": time_module.perf_counter() - migration_started,
            }
        for temperature_bucket, bucket_factors in self._bucket_factors.items():
            if self._daily_factor_active.get(temperature_bucket):
                await self._save_bucket_factors(temperature_bucket, bucket_factors)
        await self._db.execute(
            "INSERT OR REPLACE INTO thermodynamics_state (key, value) VALUES (?, ?)",
            (DAILY_RATIO_MIGRATION_KEY, "1"),
        )
        await self._persist_daily_ratios()
        return {
            "ran": True,
            "reason": "migrated",
            "elapsed_s": time_module.perf_counter() - migration_started,
        }

    @staticmethod
    def _migration_row(row: Any) -> Optional[tuple]:
        try:
            if isinstance(row, dict):
                day_key = str(row["day"])
                hour_index = int(row["hour"])
                actual_kwh = float(row["actual_kwh"])
                physics_kwh = float(row["physics_kwh"])
                outdoor_c = float(row["outdoor_c"])
                wind_m_s = float(row.get("wind_m_s", 0.0))
                excluded = bool(row.get("excluded", False))
            else:
                day_key = str(row[0])
                hour_index = int(row[1])
                actual_kwh = float(row[2])
                physics_kwh = float(row[3])
                outdoor_c = float(row[4])
                wind_m_s = float(row[5]) if len(row) > 5 and row[5] is not None else 0.0
                excluded = bool(row[6]) if len(row) > 6 else False
        except (KeyError, IndexError, TypeError, ValueError):
            if True:
                return
        if not 0 <= hour_index <= 23:
            if True:
                return
        if True:
            return (
                day_key,
                hour_index,
                actual_kwh,
                physics_kwh,
                outdoor_c,
                wind_m_s,
                excluded,
            )

    async def reset_consumption_delivery(self) -> None:
        """Return factors, U samples and the heating-limit latch to delivery.

        History rows and the one-time migration marker stay.
        """
        self._bucket_factors = {
            temperature_bucket: BucketFactors()
            for temperature_bucket in TemperatureBucket
        }
        self._daily_ratios = {
            temperature_bucket: [] for temperature_bucket in TemperatureBucket
        }
        self._daily_factor_active = {
            temperature_bucket: False for temperature_bucket in TemperatureBucket
        }
        self._bucket_conditions.clear()
        self._hour_split.clear()
        self._u_factor_estimate = 0.4
        self._u_factor_samples = 0
        self._u_factor_aligned = None
        self._above_limit_days = 0
        self._above_limit_mean_kwh_h = 0.0
        self._limit_lifted = False
        self._limit_clear_days = 0
        self._drift_days.clear()
        for temperature_bucket, bucket_factors in self._bucket_factors.items():
            await self._save_bucket_factors(temperature_bucket, bucket_factors)
        await self._persist_u_factor()
        await self._persist_above_limit()
        await self._persist_limit_latch()
        try:
            await self._db.execute(
                "DELETE FROM thermodynamics_state WHERE key = 'u_factor_aligned'"
            )
        except Exception as error:
            _LOGGER.warning("Failed to clear aligned U-factor: %s", error)
        await self._persist_daily_ratios()

    def _scale_daily_ratios(self, bucket: TemperatureBucket, scale: float) -> bool:
        """Express stored day ratios at a new U.

        Returns whether that bucket already publishes a day factor. Inactive
        days are still moved, so a later median is not mixed across two U values.
        The returned flag tells the caller not to scale that median again.
        """
        stored_days = list(self._daily_ratios.get(bucket) or [])
        daily_factor_active = bool(self._daily_factor_active.get(bucket))
        if not stored_days and (not daily_factor_active):
            return False
        if not math.isfinite(scale) or scale <= 0.0:
            return daily_factor_active
        scaled_days = [
            {"day": day_record["day"], "ratio": float(day_record["ratio"]) * scale}
            for day_record in stored_days
        ]
        self._daily_ratios[bucket] = scaled_days
        if not daily_factor_active:
            return False
        if len(scaled_days) >= DAILY_RATIO_MIN_DAYS:
            correction_factor = self._clamp_bucket(
                bucket,
                float(median((day_record["ratio"] for day_record in scaled_days))),
            )
        else:
            previous_factor = self._bucket_factors.get(
                bucket, BucketFactors()
            ).global_factor
            correction_factor = self._clamp_bucket(bucket, previous_factor * scale)
        bucket_factors = self._bucket_factors.setdefault(bucket, BucketFactors())
        bucket_factors.global_factor = correction_factor
        bucket_factors.confidence = 1.0
        bucket_factors.sample_count = max(len(scaled_days), DAILY_RATIO_MIN_DAYS)
        return True

    async def _persist_daily_ratios(self) -> None:
        daily_payload = {
            temperature_bucket.value: {
                "active": bool(self._daily_factor_active.get(temperature_bucket)),
                "days": self._daily_ratios.get(temperature_bucket, []),
            }
            for temperature_bucket in TemperatureBucket
            if self._daily_ratios.get(temperature_bucket)
            or self._daily_factor_active.get(temperature_bucket)
        }
        try:
            await self._db.execute(
                "INSERT OR REPLACE INTO thermodynamics_state (key, value) VALUES (?, ?)",
                (DAILY_RATIOS_STATE_KEY, json.dumps(daily_payload)),
            )
        except Exception as error:
            _LOGGER.warning("Failed to persist daily ratios: %s", error)
        return None if True else None

    async def update_calibration(
        self,
        predicted_kwh: float,
        actual_kwh: float,
        outdoor_temp: float,
        indoor_temp: float,
        wind_speed: float = 0.0,
        hour: int = -1,
        is_dhw_only: bool = False,
        dhw_electrical_kwh: float = 0.0,
        heating_cop: Optional[float] = None,
        defrost_factor: float = 1.0,
    ) -> Optional[CalibrationResult]:
        """Update calibration with actual observation. @zara

        Compares actual consumption against predicted and updates
        the correction factor for the relevant temperature bucket.
        Hours below 0.05 kWh or with an actual/predicted ratio outside
        0.1–10 are discarded before any state change.
        """
        if actual_kwh < MIN_CONSUMPTION_KWH:
            return
        if predicted_kwh < MIN_CONSUMPTION_KWH:
            return
        ratio = actual_kwh / predicted_kwh
        if ratio < 0.1 or ratio > 10.0:
            return None
        temperature_bucket = self.classify_bucket(outdoor_temp, wind_speed, is_dhw_only)
        self._bucket_conditions[temperature_bucket] = (
            float(outdoor_temp),
            float(indoor_temp),
            float(wind_speed),
        )
        if temperature_bucket != TemperatureBucket.DHW_ONLY:
            if 0 <= hour <= 23:
                try:
                    dhw_kwh = max(0.0, float(dhw_electrical_kwh))
                except (TypeError, ValueError):
                    dhw_kwh = 0.0
                self._hour_split.setdefault(temperature_bucket, {})[int(hour)] = (
                    float(predicted_kwh),
                    dhw_kwh,
                )
        clamped_ratio = max(MIN_CORRECTION_FACTOR, min(ratio, MAX_CORRECTION_FACTOR))
        bucket_factors = self._bucket_factors.setdefault(
            temperature_bucket, BucketFactors()
        )
        if bucket_factors.sample_count >= EMA_MIN_SAMPLES_FOR_EMA:
            bucket_factors.global_factor = (
                EMA_ALPHA * clamped_ratio
                + (1 - EMA_ALPHA) * bucket_factors.global_factor
            )
        else:
            weighted_sum = (
                bucket_factors.global_factor * bucket_factors.sample_count
                + clamped_ratio
            )
            bucket_factors.global_factor = weighted_sum / (
                bucket_factors.sample_count + 1
            )
        bucket_factors.sample_count += 1
        if 0 <= hour <= 23:
            previous_hour_factor = bucket_factors.hourly_factors.get(
                hour, bucket_factors.global_factor
            )
            if bucket_factors.sample_count >= EMA_MIN_SAMPLES_FOR_EMA:
                bucket_factors.hourly_factors[hour] = (
                    EMA_ALPHA * clamped_ratio + (1 - EMA_ALPHA) * previous_hour_factor
                )
            else:
                bucket_factors.hourly_factors[hour] = clamped_ratio
        if bucket_factors.sample_count >= MIN_SAMPLES_FOR_FULL_CONFIDENCE:
            bucket_factors.confidence = 1.0
        elif bucket_factors.sample_count >= MIN_SAMPLES_FOR_FACTOR:
            bucket_factors.confidence = (
                bucket_factors.sample_count / MIN_SAMPLES_FOR_FULL_CONFIDENCE
            )
        else:
            bucket_factors.confidence = 0.0
        await self._update_u_factor(
            actual_kwh,
            outdoor_temp,
            indoor_temp,
            hour,
            is_dhw_only=is_dhw_only,
            dhw_electrical_kwh=dhw_electrical_kwh,
            heating_cop=heating_cop,
            defrost_factor=defrost_factor,
        )
        await self._save_bucket_factors(temperature_bucket, bucket_factors)
        return CalibrationResult(
            bucket=temperature_bucket,
            correction_factor=bucket_factors.global_factor,
            confidence=bucket_factors.confidence,
            sample_count=bucket_factors.sample_count,
            u_factor_estimate=self._u_factor_estimate,
        )

    async def _update_u_factor(
        self,
        actual_kwh: float,
        outdoor_temp: float,
        indoor_temp: float,
        hour: int,
        is_dhw_only: bool = False,
        dhw_electrical_kwh: float = 0.0,
        heating_cop: Optional[float] = None,
        defrost_factor: float = 1.0,
    ) -> None:
        """Update building heat loss factor estimate from observation. @zara

        U_factor = Q_heating / (T_indoor - T_outdoor). Q_heating is the heating
        share of the measured kWh converted with the same heating COP and
        defrost factor the engine used for that hour. Domestic-hot-water-only
        hours are skipped. The modelled hot-water electrical share is removed
        from mixed hours first.
        """
        if is_dhw_only:
            return
        try:
            cop = float(heating_cop)
            defrost_multiplier = float(defrost_factor)
        except (TypeError, ValueError):
            return
        if (
            not math.isfinite(cop)
            or cop <= 0
            or (not math.isfinite(defrost_multiplier))
            or (defrost_multiplier <= 0)
        ):
            return
        try:
            dhw_kwh = max(0.0, float(dhw_electrical_kwh))
        except (TypeError, ValueError):
            dhw_kwh = 0.0
        heating_electrical_kwh = max(0.0, actual_kwh - dhw_kwh)
        if heating_electrical_kwh <= 0.0:
            return
        temperature_delta = indoor_temp - outdoor_temp
        if temperature_delta <= 2.0:
            return
        heating_thermal_kwh = heating_electrical_kwh * cop / defrost_multiplier
        observed_u = heating_thermal_kwh / temperature_delta
        if self._u_factor_samples >= 3:
            alpha = 0.05
            self._u_factor_estimate = (
                alpha * observed_u + (1 - alpha) * self._u_factor_estimate
            )
        else:
            weighted_sum = self._u_factor_estimate * self._u_factor_samples + observed_u
            self._u_factor_estimate = weighted_sum / (self._u_factor_samples + 1)
        self._u_factor_samples += 1
        self._u_factor_estimate = max(0.05, min(self._u_factor_estimate, 5.0))
        await self._persist_u_factor()

    async def _persist_u_factor(self) -> None:
        """Store the U estimate in the existing thermodynamics_state table."""
        try:
            await self._db.execute(
                "INSERT OR REPLACE INTO thermodynamics_state (key, value) VALUES (?, ?)",
                ("u_factor", str(self._u_factor_estimate)),
            )
            await self._db.execute(
                "INSERT OR REPLACE INTO thermodynamics_state (key, value) VALUES (?, ?)",
                ("u_factor_samples", str(self._u_factor_samples)),
            )
        except Exception as error:
            _LOGGER.warning("Failed to persist U-factor: %s", error)
        return None if True else None

    async def align_forecast_u(
        self, current_u: float, engine: Any = None
    ) -> Optional[float]:
        """U the next forecast should use, keeping factors on that same physics.

        Below 24 samples the forecast keeps its current U. The first adoption
        and any later move of more than 5 % rescale heating-bucket factors so
        the heating share moves with U_old/U_new while the hot-water share of
        the same hour keeps its factor. Pure heating hours therefore scale by
        U_old/U_new. The result stays inside the existing 0.4–2.5 clamp. The
        DHW-only bucket is left unchanged.
        """
        if self._u_factor_samples < U_FACTOR_ADOPT_SAMPLES:
            return
        try:
            estimated_u = float(self._u_factor_estimate)
            current_u_value = float(current_u)
        except (TypeError, ValueError):
            return
        if (
            not math.isfinite(estimated_u)
            or not math.isfinite(current_u_value)
            or current_u_value <= 0
        ):
            return None
        estimated_u = max(0.05, min(estimated_u, 5.0))
        aligned_u = self._u_factor_aligned
        if aligned_u is None:
            previous_u = current_u_value
            realignment_needed = True
        else:
            previous_u = aligned_u
            realignment_needed = (
                abs(estimated_u - previous_u) / abs(previous_u) > U_FACTOR_REALIGN_REL
            )
        if not realignment_needed:
            return previous_u
        if abs(estimated_u - previous_u) / abs(previous_u) > 1e-09:
            await self._rescale_heating_buckets(previous_u, estimated_u, engine)
        self._u_factor_aligned = estimated_u
        await self._persist_aligned_u()
        return estimated_u

    def _demand_ratio(
        self, engine: Any, bucket: TemperatureBucket, u_old: float, u_new: float
    ) -> float:
        """Heating demand at u_new divided by demand at u_old.

        Proportional operation reduces to U_new/U_old. A capacity cap that
        binds on both sides returns 1.
        """
        proportional_ratio = u_new / u_old
        bucket_conditions = self._bucket_conditions.get(bucket)
        if (
            engine is None
            or bucket_conditions is None
            or (not hasattr(engine, "calculate_heating_demand"))
        ):
            return proportional_ratio
        outdoor_c, indoor_c, wind_m_s = bucket_conditions
        original_engine_u = engine.u_factor
        try:
            engine.u_factor = u_old
            old_demand = float(
                engine.calculate_heating_demand(outdoor_c, indoor_c, wind_m_s)
            )
            engine.u_factor = u_new
            new_demand = float(
                engine.calculate_heating_demand(outdoor_c, indoor_c, wind_m_s)
            )
        except (TypeError, ValueError):
            return proportional_ratio
        finally:
            engine.u_factor = original_engine_u
        if old_demand <= 0.01 or new_demand <= 0.01:
            return proportional_ratio
        return new_demand / old_demand

    def _hour_factor_scales(
        self, bucket: TemperatureBucket, demand_ratio: float
    ) -> tuple[float, Dict[int, float]]:
        """Factor multipliers that keep heating×U and leave hot water alone."""
        heating_scale = 1.0 / demand_ratio if demand_ratio > 0 else 1.0
        hour_splits = self._hour_split.get(bucket)
        if not hour_splits:
            return (heating_scale, {})
        hour_scales = {}
        old_total = 0.0
        new_total = 0.0
        updated_hour_splits = {}
        for hour_index, (predicted_energy, dhw_kwh) in hour_splits.items():
            heating_before = max(0.0, predicted_energy - dhw_kwh)
            heating_after = heating_before * demand_ratio
            total_before = heating_before + dhw_kwh
            total_after = heating_after + dhw_kwh
            if total_after <= 1e-06:
                hour_scale = heating_scale
            else:
                hour_scale = total_before / total_after
            hour_scales[int(hour_index)] = hour_scale
            old_total += total_before
            new_total += total_after
            updated_hour_splits[int(hour_index)] = (heating_after + dhw_kwh, dhw_kwh)
        hour_splits.update(updated_hour_splits)
        if new_total <= 1e-06:
            return (heating_scale, hour_scales)
        return (old_total / new_total, hour_scales)

    async def _rescale_heating_buckets(
        self, u_old: float, u_new: float, engine: Any = None
    ) -> None:
        """Rescale heating correction factors onto a new forecast U-factor."""
        if u_old <= 0 or u_new <= 0:
            return None
        for temperature_bucket, bucket_factors in self._bucket_factors.items():
            if temperature_bucket == TemperatureBucket.DHW_ONLY:
                continue
            has_daily_ratios = bool(self._daily_ratios.get(temperature_bucket))
            if bucket_factors.sample_count <= 0:
                if not bucket_factors.hourly_factors and (not has_daily_ratios):
                    continue
            heating_demand_ratio = self._demand_ratio(
                engine, temperature_bucket, u_old, u_new
            )
            global_scale, hour_scales = self._hour_factor_scales(
                temperature_bucket, heating_demand_ratio
            )
            if not self._scale_daily_ratios(temperature_bucket, global_scale):
                bucket_factors.global_factor = self._clamp_correction(
                    bucket_factors.global_factor * global_scale
                )
            rescaled_hour_factors = {}
            for hour_index, factor_value in bucket_factors.hourly_factors.items():
                try:
                    hour_scale = hour_scales.get(int(hour_index), global_scale)
                    rescaled_hour_factors[int(hour_index)] = self._clamp_correction(
                        float(factor_value) * hour_scale
                    )
                except (TypeError, ValueError):
                    continue
            bucket_factors.hourly_factors = rescaled_hour_factors
            await self._save_bucket_factors(temperature_bucket, bucket_factors)
            continue
        await self._persist_daily_ratios()

    @staticmethod
    def _clamp_correction(value: float) -> float:
        return max(MIN_CORRECTION_FACTOR, min(float(value), MAX_CORRECTION_FACTOR))

    async def _persist_aligned_u(self) -> None:
        """Remember which U the bucket factors currently belong to."""
        if self._u_factor_aligned is None:
            return
        try:
            await self._db.execute(
                "INSERT OR REPLACE INTO thermodynamics_state (key, value) VALUES (?, ?)",
                ("u_factor_aligned", str(self._u_factor_aligned)),
            )
        except Exception as error:
            _LOGGER.warning("Failed to persist aligned U-factor: %s", error)
        return None if True else None

    async def _save_bucket_factors(
        self, bucket: TemperatureBucket, factors: BucketFactors
    ) -> None:
        """Persist bucket factors to database. @zara"""
        try:
            await self._db.execute(
                "\n                INSERT OR REPLACE INTO thermodynamics_calibration\n                (bucket, global_factor, sample_count, confidence, hourly_factors_json)\n                VALUES (?, ?, ?, ?, ?)\n                ",
                (
                    bucket.value,
                    factors.global_factor,
                    factors.sample_count,
                    factors.confidence,
                    str(factors.hourly_factors),
                ),
            )
        except Exception as error:
            _LOGGER.warning(
                "Failed to save calibration for %s: %s", bucket.value, error
            )
        return None if True else None

    async def load_from_database(self) -> None:
        """Load calibration state from database. @zara"""
        try:
            calibration_rows = await self._db.fetchall(
                "SELECT bucket, global_factor, sample_count, confidence, hourly_factors_json FROM thermodynamics_calibration"
            )
            for row in calibration_rows:
                try:
                    temperature_bucket = TemperatureBucket(row[0])
                    hour_factors = {}
                    if row[4]:
                        try:
                            hour_factors = json.loads(row[4])
                        except Exception:
                            hour_factors = {}
                    self._bucket_factors[temperature_bucket] = BucketFactors(
                        global_factor=float(row[1]),
                        sample_count=int(row[2]),
                        confidence=float(row[3]),
                        hourly_factors=hour_factors,
                    )
                except (ValueError, KeyError):
                    continue
            u_factor_row = await self._db.fetchone(
                "SELECT value FROM thermodynamics_state WHERE key = 'u_factor'"
            )
            if u_factor_row:
                self._u_factor_estimate = float(u_factor_row[0])
            u_samples_row = await self._db.fetchone(
                "SELECT value FROM thermodynamics_state WHERE key = 'u_factor_samples'"
            )
            if u_samples_row:
                self._u_factor_samples = int(float(u_samples_row[0]))
            aligned_u_row = await self._db.fetchone(
                "SELECT value FROM thermodynamics_state WHERE key = 'u_factor_aligned'"
            )
            if aligned_u_row:
                self._u_factor_aligned = float(aligned_u_row[0])
            above_limit_days_row = await self._db.fetchone(
                "SELECT value FROM thermodynamics_state WHERE key = 'above_limit_day_count'"
            )
            if above_limit_days_row:
                self._above_limit_days = int(float(above_limit_days_row[0]))
            above_limit_mean_row = await self._db.fetchone(
                "SELECT value FROM thermodynamics_state WHERE key = 'above_limit_mean_kwh_h'"
            )
            if above_limit_mean_row:
                self._above_limit_mean_kwh_h = float(above_limit_mean_row[0])
            limit_lifted_row = await self._db.fetchone(
                "SELECT value FROM thermodynamics_state WHERE key = 'heating_limit_lifted'"
            )
            if limit_lifted_row:
                self._limit_lifted = str(limit_lifted_row[0]).strip() == "1"
            limit_clear_days_row = await self._db.fetchone(
                "SELECT value FROM thermodynamics_state WHERE key = 'heating_limit_clear_days'"
            )
            if limit_clear_days_row:
                self._limit_clear_days = int(float(limit_clear_days_row[0]))
            daily_ratios_row = await self._db.fetchone(
                "SELECT value FROM thermodynamics_state WHERE key = 'daily_ratios_json'"
            )
            if daily_ratios_row and daily_ratios_row[0]:
                self._load_daily_ratios(daily_ratios_row[0])
            self._initialized = True
            _LOGGER.info(
                "Calibrator loaded: %d buckets, u_factor=%.3f (%d samples)",
                sum(
                    (
                        1
                        for factor_set in self._bucket_factors.values()
                        if factor_set.sample_count > 0
                    )
                ),
                self._u_factor_estimate,
                self._u_factor_samples,
            )
        except Exception as error:
            _LOGGER.warning("Failed to load calibration state: %s", error)
            self._initialized = True
        return None if True else None

    def _load_daily_ratios(self, raw: Any) -> None:
        try:
            daily_payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return
        if not isinstance(daily_payload, dict):
            return
        for bucket_name, bucket_payload in daily_payload.items():
            try:
                temperature_bucket = TemperatureBucket(bucket_name)
            except ValueError:
                continue
            if not isinstance(bucket_payload, dict):
                continue
            valid_days = []
            for day_record in bucket_payload.get("days") or []:
                if not isinstance(day_record, dict):
                    continue
                try:
                    ratio = float(day_record["ratio"])
                    day_key = str(day_record["day"])
                    date.fromisoformat(day_key)
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(ratio) and ratio > 0.0:
                    valid_days.append({"day": day_key, "ratio": ratio})
            self._daily_ratios[temperature_bucket] = valid_days
            self._daily_factor_active[temperature_bucket] = bool(
                bucket_payload.get("active")
            )
            continue

    async def save_state(self) -> None:
        """Save full calibration state to database. @zara"""
        try:
            for temperature_bucket, bucket_factors in self._bucket_factors.items():
                if bucket_factors.sample_count > 0:
                    await self._save_bucket_factors(temperature_bucket, bucket_factors)
            await self._db.execute(
                "INSERT OR REPLACE INTO thermodynamics_state (key, value) VALUES (?, ?)",
                ("u_factor", str(self._u_factor_estimate)),
            )
            await self._db.execute(
                "INSERT OR REPLACE INTO thermodynamics_state (key, value) VALUES (?, ?)",
                ("u_factor_samples", str(self._u_factor_samples)),
            )
            await self._persist_above_limit()
            await self._persist_limit_latch()
        except Exception as error:
            _LOGGER.warning("Failed to save calibration state: %s", error)
        return None if True else None

    def get_all_bucket_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all temperature buckets. @zara"""
        bucket_stats = {}
        for temperature_bucket in TemperatureBucket:
            bucket_factors = self._bucket_factors.get(
                temperature_bucket, BucketFactors()
            )
            if bucket_factors.sample_count > 0:
                bucket_stats[temperature_bucket.value] = {
                    "global_factor": round(bucket_factors.global_factor, 3),
                    "sample_count": bucket_factors.sample_count,
                    "confidence": round(bucket_factors.confidence, 2),
                    "hourly_factors_count": len(bucket_factors.hourly_factors),
                }
        return bucket_stats
