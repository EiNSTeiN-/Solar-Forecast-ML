# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Physics Calibrator Module.
Self-learning calibrator for Physics Engine using database storage.
@zara
"""
from __future__ import annotations
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Any, Dict, List, Optional
from ..data.db_manager import DatabaseManager
from ..core.core_helpers import SafeDateTimeUtil, get_season
from .physics_calibration_telemetry import (
    PHYSICS_CALIBRATION_GHI_FORECAST_SCALE_CONTRACT,
    PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT,
    PHYSICS_CALIBRATION_LEGACY_CONTRACT,
    PHYSICS_CALIBRATION_POA_DNI_IAM_CONTRACT,
    PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT,
    PHYSICS_CALIBRATION_RAW_FORECAST_V1_CONTRACT,
    PHYSICS_CALIBRATION_RAW_FORECAST_V2_CONTRACT,
    PHYSICS_CALIBRATION_SENSOR_GHI_CONTRACT,
    PHYSICS_CALIBRATION_POST_CAP_CONTRACTS,
    RAW_LEARN_DAYS,
    PHYSICS_REF_KIND_FORECAST,
    PHYSICS_REF_KIND_GHI_SCALED,
    advantage_factor,
    clamp_union_hourly,
    ema_raw_factor,
    group_best_method,
    hourly_factor_shape,
    is_raw_forecast_contract,
    recommended_tfs_weight,
    regime_best_method,
    poa_contract_hour_is_learnable,
    publish_raw_factor,
    smoothed_mae_rows,
    select_ghi_forecast_scale_contract,
    select_ghi_tilt_poa_contract,
    select_poa_dni_iam_contract,
    select_post_cap_learning_contract,
    select_raw_forecast_contract,
    should_hold_post_cap_rearm,
    should_rebuild_factors_from_history,
)
from .physics_sun_shadow import SunShadowBook, direct_share_of

_LOGGER = logging.getLogger(__name__)
MIN_PRODUCTION_KWH = 0.01
MIN_PRODUCTION_KWH_MORNING = 0.003
MAX_CORRECTION_FACTOR = 4.0
MIN_CORRECTION_FACTOR = 0.3
MAX_BUCKET_CORRECTION_FACTOR = 1.5
MIN_BUCKET_CORRECTION_FACTOR = 0.67
BAD_WEATHER_BUCKET_MAX_FACTOR = 1.18
OVERCAST_BUCKET_MAX_FACTOR = 1.1
DENSE_OVERCAST_RUNTIME_MAX_FACTOR = 1.04
ROLLING_WINDOW_DAYS = 21
MIN_SAMPLES_FOR_FACTOR = 1
MIN_SAMPLES_FOR_FULL_CONFIDENCE = 7
EMA_ALPHA = 0.22
EMA_MIN_SAMPLES_FOR_EMA = 3
TILT_REARM_MIN_DAYS = 2
BUCKET_CLEAR_MAX = 25
BUCKET_FAIR_MAX = 50
BUCKET_CLOUDY_MAX = 75
BUCKET_RAIN_THRESHOLD = 0.5
FROST_SCORE_THRESHOLD = 5
LOW_SUN_MIN_ELEVATION = 5.0
LOW_SUN_MAX_ELEVATION = 15.0
STRONG_OVERCAST_CLOUD_THRESHOLD = 85.0
PEAK_SOLAR_MIN_ELEVATION = 12.0
PEAK_SOLAR_START_HOUR = 10
PEAK_SOLAR_END_HOUR = 16


class WeatherBucket(Enum):
    """Weather condition buckets for calibration. @zara"""

    CLEAR = "clear"
    CLEAR_LOW_SUN = "clear_low_sun"
    FAIR = "fair"
    FAIR_LOW_SUN = "fair_low_sun"
    CLOUDY = "cloudy"
    CLOUDY_LOW_SUN = "cloudy_low_sun"
    OVERCAST = "overcast"
    RAINY = "rainy"
    RAINY_LOW_SUN = "rainy_low_sun"
    SNOWY = "snowy"
    SNOWING = "snowing"
    SNOWING_LOW_SUN = "snowing_low_sun"
    FOG = "fog"
    FOG_LOW_SUN = "fog_low_sun"
    FROST = "frost"
    FROST_LOW_SUN = "frost_low_sun"


@dataclass
class BucketFactors:
    """Calibration factors for a specific weather bucket. @zara"""

    global_factor: float = 1.0
    hourly_factors: Dict[int, float] = field(default_factory=dict)
    sample_count: int = 0
    confidence: float = 0.0


@dataclass
class HourlyCalibration:
    """Calibration data for a specific hour. @zara"""

    hour: int
    physics_kwh: float
    actual_kwh: float
    ratio: float
    date: str
    group_name: Optional[str] = None


@dataclass
class GroupCalibrationFactors:
    """Calibration factors for a panel group. @zara"""

    group_name: str
    global_factor: float = 1.0
    hourly_factors: Dict[int, float] = field(default_factory=dict)
    bucket_factors: Dict[str, BucketFactors] = field(default_factory=dict)
    sample_count: int = 0
    confidence: float = 0.0
    last_updated: Optional[str] = None
    learning_contract_version: str = PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT
    poa_contract_stamped_at: Optional[str] = None
    rearm_learning_days: Optional[int] = None
    rearm_pending_factor: Optional[float] = None
    raw_damp_pending: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CalibrationResult:
    """Result of calibration calculation. @zara"""

    success: bool
    message: str
    groups_calibrated: int = 0
    total_samples: int = 0
    avg_correction_factor: float = 1.0


class PhysicsCalibrator:
    """Self-learning calibrator for Physics Engine using database storage. @zara"""

    def __init__(self, db_manager: DatabaseManager):
        "Initialize the calibrator. @zara\n\nArgs:\n    db_manager: Database manager instance\n"
        self._db = db_manager
        self._factors = {}
        self._history = []
        self._sun_shadow = SunShadowBook()
        _LOGGER.debug("PhysicsCalibrator initialized with database storage")

    async def async_init(self) -> bool:
        """Async initialization - load existing calibration data from database. @zara"""
        try:
            await self._load_config()
            await self._apply_post_cap_gate_contract()
            await self._apply_poa_dni_iam_contract()
            await self._apply_ghi_forecast_scale_contract()
            await self._apply_ghi_tilt_poa_contract()
            await self._apply_raw_forecast_contract()
            if not self._sun_shadow.has_samples():
                await self._load_sun_shadow()
            await self._load_history()
            if self._factors:
                group_names = list(self._factors.keys())
                _LOGGER.debug(
                    f"PhysicsCalibrator loaded calibration for {len(group_names)} groups: {group_names}"
                )
            else:
                _LOGGER.info("PhysicsCalibrator: No existing calibration data found")
            if True:
                return True
        except Exception as error:
            _LOGGER.error(f"PhysicsCalibrator initialization failed: {error}")
            return False

    async def _load_config(self) -> None:
        """Load calibration config from database. @zara"""
        try:
            rows = await self._db.fetchall(
                "SELECT group_name, global_factor, sample_count, confidence,\n                          last_updated, learning_contract_version,\n                          poa_contract_stamped_at, rearm_learning_days,\n                          rearm_pending_factor, raw_damp_pending\n                   FROM physics_calibration_groups"
            )
            for row in rows:
                group_name = row[0]
                hourly_factors = {}
                hourly_rows = await self._db.fetchall(
                    "SELECT hour, factor FROM physics_calibration_hourly WHERE group_name = ?",
                    (group_name,),
                )
                for hourly_row in hourly_rows:
                    hourly_factors[hourly_row[0]] = hourly_row[1]
                bucket_factors = {}
                bucket_rows = await self._db.fetchall(
                    "SELECT bucket_name, global_factor, sample_count, confidence\n                       FROM physics_calibration_buckets WHERE group_name = ?",
                    (group_name,),
                )
                for bucket_row in bucket_rows:
                    bucket_name = bucket_row[0]
                    bucket_hourly_factors = {}
                    bucket_hourly_rows = await self._db.fetchall(
                        "SELECT hour, factor FROM physics_calibration_bucket_hourly\n                           WHERE group_name = ? AND bucket_name = ?",
                        (group_name, bucket_name),
                    )
                    for bucket_hourly_row in bucket_hourly_rows:
                        bucket_hourly_factors[bucket_hourly_row[0]] = bucket_hourly_row[
                            1
                        ]
                    bucket_factors[bucket_name] = BucketFactors(
                        global_factor=self._clamp_bucket_factor(
                            bucket_name, bucket_row[1]
                        ),
                        hourly_factors=bucket_hourly_factors,
                        sample_count=bucket_row[2],
                        confidence=bucket_row[3],
                    )
                self._factors[group_name] = GroupCalibrationFactors(
                    group_name=group_name,
                    global_factor=row[1],
                    hourly_factors=hourly_factors,
                    bucket_factors=bucket_factors,
                    sample_count=row[2],
                    confidence=row[3],
                    last_updated=str(row[4]) if row[4] else None,
                    learning_contract_version=(
                        row[5] if row[5] else PHYSICS_CALIBRATION_LEGACY_CONTRACT
                    ),
                    poa_contract_stamped_at=str(row[6]) if row[6] else None,
                    rearm_learning_days=None if row[7] is None else int(row[7]),
                    rearm_pending_factor=None if row[8] is None else float(row[8]),
                    raw_damp_pending=self._parse_raw_pending(
                        row[9] if len(row) > 9 else None
                    ),
                )
                continue
            bucket_count = sum(
                (
                    len(factor_set.bucket_factors)
                    for factor_set in self._factors.values()
                )
            )
            _LOGGER.debug(
                f"Loaded calibration config: {len(self._factors)} groups, {bucket_count} bucket factors"
            )
        except Exception as error:
            _LOGGER.warning(f"Could not load calibration config: {error}")
            self._factors = {}
        return None if True else None

    async def _apply_post_cap_gate_contract(self) -> None:
        """Stamp post-cap contracts and wipe cap-absorbed live factors once."""
        if not self._factors:
            return
        groups_to_update = [
            group_factors
            for group_factors in self._factors.values()
            if group_factors.learning_contract_version
            not in PHYSICS_CALIBRATION_POST_CAP_CONTRACTS
        ]
        if not groups_to_update:
            return
        cutoff_date = (
            (SafeDateTimeUtil.now() - timedelta(days=ROLLING_WINDOW_DAYS))
            .date()
            .isoformat()
        )
        changed = False
        for group_factors in groups_to_update:
            forecast_sample_count = 0
            ghi_sample_count = 0
            try:
                rows = await self._db.fetchall(
                    "SELECT physics_ref_kind, COUNT(*)\n                       FROM physics_calibration_samples\n                       WHERE group_name = ? AND date >= ?\n                       GROUP BY physics_ref_kind",
                    (group_factors.group_name, cutoff_date),
                )
            except Exception as error:
                _LOGGER.warning(
                    "Post-cap contract sample count failed for %s: %s",
                    group_factors.group_name,
                    error,
                )
                rows = []
            for row in rows or ():
                reference_kind = row[0]
                try:
                    sample_count = int(row[1] or 0)
                except (TypeError, ValueError):
                    sample_count = 0
                if reference_kind == PHYSICS_REF_KIND_FORECAST:
                    forecast_sample_count = sample_count
                elif reference_kind == PHYSICS_REF_KIND_GHI_SCALED:
                    ghi_sample_count = sample_count
            contract_version, reset_factors = select_post_cap_learning_contract(
                forecast_sample_count, ghi_sample_count
            )
            if reset_factors:
                group_factors.global_factor = 1.0
                group_factors.hourly_factors = {}
                group_factors.bucket_factors = {}
                group_factors.sample_count = 0
                group_factors.confidence = 0.0
                _LOGGER.info(
                    "Reset live physics factors for %s after cap gate (forecast_physics=%d, ghi_scaled=%d)",
                    group_factors.group_name,
                    forecast_sample_count,
                    ghi_sample_count,
                )
            group_factors.learning_contract_version = contract_version
            changed = True
        if changed:
            await self._save_config()
        return None if True else None

    async def _apply_poa_dni_iam_contract(self) -> None:
        """Stamp the POA contract and wipe live factors learned on older physics.

        Runs once per group. A restart sees the new version and does not wipe
        again. Old history rows stay in the database; rebuild skips this
        contract the same way it skips the forecast post-cap contract.
        """
        if not self._factors:
            return
        changed = False
        stamp = SafeDateTimeUtil.now().isoformat()
        for group_factors in self._factors.values():
            contract_version, reset_factors = select_poa_dni_iam_contract(
                group_factors.learning_contract_version
            )
            if not reset_factors:
                if not group_factors.poa_contract_stamped_at:
                    group_factors.poa_contract_stamped_at = stamp
                    changed = True
                continue
            group_factors.global_factor = 1.0
            group_factors.hourly_factors = {}
            group_factors.bucket_factors = {}
            group_factors.sample_count = 0
            group_factors.confidence = 0.0
            group_factors.learning_contract_version = contract_version
            group_factors.poa_contract_stamped_at = stamp
            changed = True
            _LOGGER.info(
                "Reset live physics factors for %s onto %s at %s",
                group_factors.group_name,
                contract_version,
                stamp,
            )
        if changed:
            await self._save_config()
        return None if True else None

    async def _apply_ghi_forecast_scale_contract(self) -> None:
        """Stamp the forecast-scale contract. Wipe sensor-scale groups once.

        Sample reads happen before the write. The wipe and the version update
        commit together; a failure leaves the previous version in place.
        """
        if not self._factors:
            return
        accepted_contracts = (
            PHYSICS_CALIBRATION_GHI_FORECAST_SCALE_CONTRACT,
            PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT,
        )
        groups_to_update = [
            group_factors
            for group_factors in self._factors.values()
            if group_factors.learning_contract_version not in accepted_contracts
        ]
        if not groups_to_update:
            return
        cutoff_date = (
            (SafeDateTimeUtil.now() - timedelta(days=ROLLING_WINDOW_DAYS))
            .date()
            .isoformat()
        )
        planned_updates = []
        for group_factors in groups_to_update:
            try:
                rows = await self._db.fetchall(
                    "SELECT physics_ref_kind, COUNT(*)\n                       FROM physics_calibration_samples\n                       WHERE group_name = ? AND date >= ?\n                       GROUP BY physics_ref_kind",
                    (group_factors.group_name, cutoff_date),
                )
            except Exception as error:
                _LOGGER.warning(
                    "GHI forecast-scale contract sample count failed for %s: %s",
                    group_factors.group_name,
                    error,
                )
                return
            ghi_sample_count = 0
            for row in rows or ():
                if row[0] != PHYSICS_REF_KIND_GHI_SCALED:
                    continue
                try:
                    ghi_sample_count = int(row[1] or 0)
                except (TypeError, ValueError):
                    ghi_sample_count = 0
                    continue
            contract_version, reset_factors = select_ghi_forecast_scale_contract(
                group_factors.learning_contract_version, ghi_sample_count
            )
            planned_updates.append(
                (
                    group_factors.group_name,
                    contract_version,
                    reset_factors,
                    ghi_sample_count,
                )
            )
        if not planned_updates:
            return
        stamp = SafeDateTimeUtil.now()
        try:
            async with self._db.transaction():
                for (
                    group_name,
                    contract_version,
                    reset_factors,
                    stored_ghi_sample_count,
                ) in planned_updates:
                    if reset_factors:
                        await self._db.execute(
                            "UPDATE physics_calibration_groups\n                               SET global_factor = 1.0,\n                                   sample_count = 0,\n                                   confidence = 0.0,\n                                   learning_contract_version = ?,\n                                   last_updated = ?\n                               WHERE group_name = ?",
                            (contract_version, stamp, group_name),
                            auto_commit=False,
                        )
                        await self._db.execute(
                            "DELETE FROM physics_calibration_hourly WHERE group_name = ?",
                            (group_name,),
                            auto_commit=False,
                        )
                        await self._db.execute(
                            "DELETE FROM physics_calibration_bucket_hourly WHERE group_name = ?",
                            (group_name,),
                            auto_commit=False,
                        )
                        await self._db.execute(
                            "DELETE FROM physics_calibration_buckets WHERE group_name = ?",
                            (group_name,),
                            auto_commit=False,
                        )
                    else:
                        await self._db.execute(
                            "UPDATE physics_calibration_groups\n                               SET learning_contract_version = ?\n                               WHERE group_name = ?",
                            (contract_version, group_name),
                            auto_commit=False,
                        )
        except Exception as error:
            _LOGGER.warning("GHI forecast-scale contract commit failed: %s", error)
            return
        for (
            group_name,
            contract_version,
            reset_factors,
            ghi_sample_count,
        ) in planned_updates:
            group_factors = self._factors[group_name]
            if reset_factors:
                group_factors.global_factor = 1.0
                group_factors.hourly_factors = {}
                group_factors.bucket_factors = {}
                group_factors.sample_count = 0
                group_factors.confidence = 0.0
                group_factors.last_updated = stamp.isoformat()
                _LOGGER.info(
                    "Reset live physics factors for %s onto %s (ghi_scaled=%d)",
                    group_name,
                    contract_version,
                    ghi_sample_count,
                )
            group_factors.learning_contract_version = contract_version

    async def _apply_ghi_tilt_poa_contract(self) -> None:
        """Stamp the tilt POA contract. Wipe sensor-scale groups once.

        Sample reads happen before the write. The wipe, the rearm marker and
        the version update commit together. ``poa_contract_stamped_at`` is
        not written.
        """
        if not self._factors:
            return
        groups_to_update = [
            group_factors
            for group_factors in self._factors.values()
            if group_factors.learning_contract_version
            != PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT
        ]
        if not groups_to_update:
            return
        cutoff_date = (
            (SafeDateTimeUtil.now() - timedelta(days=ROLLING_WINDOW_DAYS))
            .date()
            .isoformat()
        )
        planned_updates = []
        for group_factors in groups_to_update:
            try:
                rows = await self._db.fetchall(
                    "SELECT physics_ref_kind, COUNT(*)\n                       FROM physics_calibration_samples\n                       WHERE group_name = ? AND date >= ?\n                       GROUP BY physics_ref_kind",
                    (group_factors.group_name, cutoff_date),
                )
            except Exception as error:
                _LOGGER.warning(
                    "GHI tilt POA contract sample count failed for %s: %s",
                    group_factors.group_name,
                    error,
                )
                return
            ghi_sample_count = 0
            for row in rows or ():
                if row[0] != PHYSICS_REF_KIND_GHI_SCALED:
                    continue
                try:
                    ghi_sample_count = int(row[1] or 0)
                except (TypeError, ValueError):
                    ghi_sample_count = 0
                    continue
            contract_version, reset_factors = select_ghi_tilt_poa_contract(
                group_factors.learning_contract_version, ghi_sample_count
            )
            planned_updates.append(
                (
                    group_factors.group_name,
                    contract_version,
                    reset_factors,
                    ghi_sample_count,
                )
            )
        if not planned_updates:
            return
        stamp = SafeDateTimeUtil.now()
        try:
            async with self._db.transaction():
                for (
                    group_name,
                    contract_version,
                    reset_factors,
                    stored_ghi_sample_count,
                ) in planned_updates:
                    if reset_factors:
                        await self._db.execute(
                            "UPDATE physics_calibration_groups\n                               SET global_factor = 1.0,\n                                   sample_count = 0,\n                                   confidence = 0.0,\n                                   learning_contract_version = ?,\n                                   last_updated = ?,\n                                   rearm_learning_days = 0,\n                                   rearm_pending_factor = NULL\n                               WHERE group_name = ?",
                            (contract_version, stamp, group_name),
                            auto_commit=False,
                        )
                        await self._db.execute(
                            "DELETE FROM physics_calibration_hourly WHERE group_name = ?",
                            (group_name,),
                            auto_commit=False,
                        )
                        await self._db.execute(
                            "DELETE FROM physics_calibration_bucket_hourly WHERE group_name = ?",
                            (group_name,),
                            auto_commit=False,
                        )
                        await self._db.execute(
                            "DELETE FROM physics_calibration_buckets WHERE group_name = ?",
                            (group_name,),
                            auto_commit=False,
                        )
                    else:
                        await self._db.execute(
                            "UPDATE physics_calibration_groups\n                               SET learning_contract_version = ?\n                               WHERE group_name = ?",
                            (contract_version, group_name),
                            auto_commit=False,
                        )
        except Exception as error:
            _LOGGER.warning("GHI tilt POA contract commit failed: %s", error)
            return
        for (
            group_name,
            contract_version,
            reset_factors,
            ghi_sample_count,
        ) in planned_updates:
            group_factors = self._factors[group_name]
            if reset_factors:
                group_factors.global_factor = 1.0
                group_factors.hourly_factors = {}
                group_factors.bucket_factors = {}
                group_factors.sample_count = 0
                group_factors.confidence = 0.0
                group_factors.rearm_learning_days = 0
                group_factors.rearm_pending_factor = None
                group_factors.last_updated = stamp.isoformat()
                _LOGGER.info(
                    "Reset live physics factors for %s onto %s (ghi_scaled=%d)",
                    group_name,
                    contract_version,
                    ghi_sample_count,
                )
            group_factors.learning_contract_version = contract_version

    async def _load_history(self) -> None:
        """Load calibration history from database. @zara"""
        try:
            cutoff_date = (
                (SafeDateTimeUtil.now() - timedelta(days=ROLLING_WINDOW_DAYS))
                .date()
                .isoformat()
            )
            rows = await self._db.fetchall(
                "SELECT date, group_name, bucket_name, hour, avg_ratio, sample_count\n                   FROM physics_calibration_history\n                   WHERE date >= ?\n                   ORDER BY date, group_name",
                (cutoff_date,),
            )
            history_by_date = {}
            for row in rows:
                history_date = str(row[0])
                group_name = row[1]
                bucket_name = row[2]
                hour = row[3]
                average_ratio = row[4]
                sample_count = row[5]
                if history_date not in history_by_date:
                    history_by_date[history_date] = {"date": history_date, "groups": {}}
                if group_name not in history_by_date[history_date]["groups"]:
                    history_by_date[history_date]["groups"][group_name] = {
                        "hourly": {},
                        "sample_count": 0,
                        "buckets": {},
                    }
                if bucket_name:
                    if (
                        bucket_name
                        not in history_by_date[history_date]["groups"][group_name][
                            "buckets"
                        ]
                    ):
                        history_by_date[history_date]["groups"][group_name]["buckets"][
                            bucket_name
                        ] = {"hourly": {}, "sample_count": 0}
                    if hour is not None:
                        history_by_date[history_date]["groups"][group_name]["buckets"][
                            bucket_name
                        ]["hourly"][str(hour)] = average_ratio
                        history_by_date[history_date]["groups"][group_name]["buckets"][
                            bucket_name
                        ]["sample_count"] += sample_count
                        continue
                    continue
                if hour is not None:
                    history_by_date[history_date]["groups"][group_name]["hourly"][
                        str(hour)
                    ] = average_ratio
                    history_by_date[history_date]["groups"][group_name][
                        "sample_count"
                    ] += sample_count
                    continue
            self._history = list(history_by_date.values())
        except Exception as error:
            _LOGGER.warning(f"Could not load calibration history: {error}")
            self._history = []
        return None if True else None

    async def _save_config(self) -> None:
        """Save calibration config to database. @zara"""
        try:
            average_global_factor = 1.0
            if self._factors:
                average_global_factor = sum(
                    (factor_set.global_factor for factor_set in self._factors.values())
                ) / len(self._factors)
            await self._db.execute(
                "INSERT INTO physics_learning_config\n                   (id, version, learned_efficiency_factor, rolling_window_days, min_samples, updated_at)\n                   VALUES (1, '3.0', ?, ?, ?, ?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       learned_efficiency_factor = excluded.learned_efficiency_factor,\n                       updated_at = excluded.updated_at",
                (
                    round(average_global_factor, 4),
                    ROLLING_WINDOW_DAYS,
                    MIN_SAMPLES_FOR_FACTOR,
                    SafeDateTimeUtil.now(),
                ),
            )
            for group_name, group_factors in self._factors.items():
                await self._db.execute(
                    "INSERT INTO physics_calibration_groups\n                       (group_name, global_factor, sample_count, confidence,\n                        last_updated, learning_contract_version,\n                        poa_contract_stamped_at, rearm_learning_days,\n                        rearm_pending_factor, raw_damp_pending)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(group_name) DO UPDATE SET\n                           global_factor = excluded.global_factor,\n                           sample_count = excluded.sample_count,\n                           confidence = excluded.confidence,\n                           last_updated = excluded.last_updated,\n                           learning_contract_version = excluded.learning_contract_version,\n                           poa_contract_stamped_at = excluded.poa_contract_stamped_at,\n                           rearm_learning_days = excluded.rearm_learning_days,\n                           rearm_pending_factor = excluded.rearm_pending_factor,\n                           raw_damp_pending = excluded.raw_damp_pending",
                    (
                        group_name,
                        round(group_factors.global_factor, 4),
                        group_factors.sample_count,
                        round(group_factors.confidence, 3),
                        SafeDateTimeUtil.now(),
                        group_factors.learning_contract_version
                        or PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT,
                        group_factors.poa_contract_stamped_at,
                        group_factors.rearm_learning_days,
                        (
                            None
                            if group_factors.rearm_pending_factor is None
                            else round(group_factors.rearm_pending_factor, 6)
                        ),
                        self._dump_raw_pending(group_factors.raw_damp_pending),
                    ),
                )
                await self._db.execute(
                    "DELETE FROM physics_calibration_hourly WHERE group_name = ?",
                    (group_name,),
                )
                for hour, hourly_factor in group_factors.hourly_factors.items():
                    await self._db.execute(
                        "INSERT INTO physics_calibration_hourly (group_name, hour, factor)\n                           VALUES (?, ?, ?)",
                        (group_name, hour, round(hourly_factor, 4)),
                    )
                await self._db.execute(
                    "DELETE FROM physics_calibration_buckets WHERE group_name = ?",
                    (group_name,),
                )
                await self._db.execute(
                    "DELETE FROM physics_calibration_bucket_hourly WHERE group_name = ?",
                    (group_name,),
                )
                for bucket_name, bucket_factor in group_factors.bucket_factors.items():
                    await self._db.execute(
                        "INSERT INTO physics_calibration_buckets\n                           (group_name, bucket_name, global_factor, sample_count, confidence)\n                           VALUES (?, ?, ?, ?, ?)",
                        (
                            group_name,
                            bucket_name,
                            round(bucket_factor.global_factor, 4),
                            bucket_factor.sample_count,
                            round(bucket_factor.confidence, 3),
                        ),
                    )
                    for hour, hourly_factor in bucket_factor.hourly_factors.items():
                        await self._db.execute(
                            "INSERT INTO physics_calibration_bucket_hourly\n                               (group_name, bucket_name, hour, factor) VALUES (?, ?, ?, ?)",
                            (group_name, bucket_name, hour, round(hourly_factor, 4)),
                        )
                continue
            _LOGGER.debug("Saved calibration config to database")
        except Exception as error:
            _LOGGER.error(f"Failed to save calibration config: {error}")
        return None if True else None

    async def _save_history(self, date: str, group_data: Dict[str, Any]) -> None:
        """Save calibration history to database. @zara"""
        try:
            for group_name, history_data in group_data.items():
                for hour_key, average_ratio in history_data.get("hourly", {}).items():
                    hour = int(hour_key)
                    await self._db.execute(
                        "INSERT INTO physics_calibration_history\n                           (date, group_name, bucket_name, hour, avg_ratio, sample_count, source)\n                           VALUES (?, ?, NULL, ?, ?, ?, 'daily_calibration')\n                           ON CONFLICT(date, group_name, bucket_name, hour) DO UPDATE SET\n                               avg_ratio = excluded.avg_ratio,\n                               sample_count = excluded.sample_count",
                        (
                            date,
                            group_name,
                            hour,
                            average_ratio,
                            history_data.get("sample_count", 1),
                        ),
                    )
                for bucket_name, bucket_data in history_data.get("buckets", {}).items():
                    for hour_key, average_ratio in bucket_data.get(
                        "hourly", {}
                    ).items():
                        hour = int(hour_key)
                        await self._db.execute(
                            "INSERT INTO physics_calibration_history\n                               (date, group_name, bucket_name, hour, avg_ratio, sample_count, source)\n                               VALUES (?, ?, ?, ?, ?, ?, 'daily_calibration')\n                               ON CONFLICT(date, group_name, bucket_name, hour) DO UPDATE SET\n                                   avg_ratio = excluded.avg_ratio,\n                                   sample_count = excluded.sample_count",
                            (
                                date,
                                group_name,
                                bucket_name,
                                hour,
                                average_ratio,
                                bucket_data.get("sample_count", 1),
                            ),
                        )
                continue
        except Exception as error:
            _LOGGER.error(f"Failed to save calibration history: {error}")
        return None if True else None

    async def _save_calibration_telemetry(
        self,
        date: str,
        sample_rows: List[Dict[str, Any]],
        clamp_hits: List[Dict[str, Any]],
    ) -> None:
        """Persist sample ledger, clamp hits, and hourly-factor spread. @zara"""
        try:
            for sample_row in sample_rows:
                await self._db.execute(
                    "INSERT INTO physics_calibration_samples\n                       (date, group_name, hour, physics_ref_kind, physics_ref_kwh,\n                        actual_kwh, unclamped_ratio, clamped_ratio, ratio_clamp_hit,\n                        bucket_name)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(date, group_name, hour) DO UPDATE SET\n                           physics_ref_kind = excluded.physics_ref_kind,\n                           physics_ref_kwh = excluded.physics_ref_kwh,\n                           actual_kwh = excluded.actual_kwh,\n                           unclamped_ratio = excluded.unclamped_ratio,\n                           clamped_ratio = excluded.clamped_ratio,\n                           ratio_clamp_hit = excluded.ratio_clamp_hit,\n                           bucket_name = excluded.bucket_name",
                    (
                        sample_row["date"],
                        sample_row["group_name"],
                        sample_row["hour"],
                        sample_row["physics_ref_kind"],
                        round(float(sample_row["physics_ref_kwh"]), 6),
                        round(float(sample_row["actual_kwh"]), 6),
                        round(float(sample_row["unclamped_ratio"]), 6),
                        round(float(sample_row["clamped_ratio"]), 6),
                        int(sample_row["ratio_clamp_hit"]),
                        sample_row.get("bucket_name"),
                    ),
                )
            await self._db.execute(
                "DELETE FROM physics_calibration_clamp_hits WHERE date = ?", (date,)
            )
            for clamp_hit in clamp_hits:
                await self._db.execute(
                    "INSERT INTO physics_calibration_clamp_hits\n                       (date, group_name, hour, bucket_name, clamp_kind,\n                        unclamped_value, clamped_value)\n                       VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        clamp_hit["date"],
                        clamp_hit["group_name"],
                        clamp_hit.get("hour"),
                        clamp_hit.get("bucket_name"),
                        clamp_hit["clamp_kind"],
                        round(float(clamp_hit["unclamped_value"]), 6),
                        round(float(clamp_hit["clamped_value"]), 6),
                    ),
                )
            for group_name, group_factors in self._factors.items():
                hourly_shape = hourly_factor_shape(group_factors.hourly_factors)
                if not hourly_shape:
                    continue
                await self._db.execute(
                    "INSERT INTO physics_calibration_hourly_shape\n                       (date, group_name, min_hour, max_hour, hour_count,\n                        min_factor, max_factor, spread)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(date, group_name) DO UPDATE SET\n                           min_hour = excluded.min_hour,\n                           max_hour = excluded.max_hour,\n                           hour_count = excluded.hour_count,\n                           min_factor = excluded.min_factor,\n                           max_factor = excluded.max_factor,\n                           spread = excluded.spread",
                    (
                        date,
                        group_name,
                        hourly_shape["min_hour"],
                        hourly_shape["max_hour"],
                        hourly_shape["hour_count"],
                        hourly_shape["min_factor"],
                        hourly_shape["max_factor"],
                        hourly_shape["spread"],
                    ),
                )
        except Exception as error:
            _LOGGER.warning("Physics calibration telemetry save skipped: %s", error)
        return None if True else None

    def _adaptive_alpha(self, current_avg: float) -> float:
        """Scale EMA alpha by deviation magnitude. @zara"""
        deviation = abs(current_avg - 1.0)
        return min(0.4, EMA_ALPHA + deviation * 0.15)

    def _get_bucket_factor_limits(
        self,
        bucket_name: str,
        cloud_cover: Optional[float] = None,
        sun_elevation: Optional[float] = None,
        hour: Optional[int] = None,
    ) -> tuple[float, float]:
        """Return safe factor limits for a learned weather bucket."""
        upper_limit = MAX_BUCKET_CORRECTION_FACTOR
        if bucket_name in (
            WeatherBucket.CLOUDY.value,
            WeatherBucket.CLOUDY_LOW_SUN.value,
        ):
            upper_limit = BAD_WEATHER_BUCKET_MAX_FACTOR
        elif bucket_name in (
            WeatherBucket.OVERCAST.value,
            WeatherBucket.RAINY.value,
            WeatherBucket.RAINY_LOW_SUN.value,
            WeatherBucket.FOG.value,
            WeatherBucket.FOG_LOW_SUN.value,
            WeatherBucket.SNOWY.value,
            WeatherBucket.SNOWING.value,
            WeatherBucket.SNOWING_LOW_SUN.value,
        ):
            upper_limit = OVERCAST_BUCKET_MAX_FACTOR
            dense_overcast = (
                cloud_cover is not None
                and cloud_cover >= STRONG_OVERCAST_CLOUD_THRESHOLD
                and (sun_elevation is not None)
                and (sun_elevation >= PEAK_SOLAR_MIN_ELEVATION)
                and (hour is not None)
                and (PEAK_SOLAR_START_HOUR <= hour <= PEAK_SOLAR_END_HOUR)
            )
            if dense_overcast:
                upper_limit = min(upper_limit, DENSE_OVERCAST_RUNTIME_MAX_FACTOR)
        return (MIN_BUCKET_CORRECTION_FACTOR, upper_limit) if True else None

    def _clamp_bucket_factor(
        self,
        bucket_name: str,
        factor: float,
        cloud_cover: Optional[float] = None,
        sun_elevation: Optional[float] = None,
        hour: Optional[int] = None,
    ) -> float:
        """Clamp bucket learning into rollout-safe limits."""
        lower_limit, upper_limit = self._get_bucket_factor_limits(
            bucket_name, cloud_cover=cloud_cover, sun_elevation=sun_elevation, hour=hour
        )
        return max(lower_limit, min(upper_limit, factor))

    def _classify_weather(
        self,
        cloud_cover: float,
        sun_elevation: Optional[float] = None,
        hour: Optional[int] = None,
        precipitation_mm: Optional[float] = None,
        snow_covered_panels: bool = False,
        visibility_m: Optional[float] = None,
        temperature_c: Optional[float] = None,
        frost_detected: Optional[str] = None,
        frost_score: Optional[int] = None,
        ghi_ratio: Optional[float] = None,
    ) -> WeatherBucket:
        """Classify weather conditions into weather bucket. @zara"""
        if snow_covered_panels:
            return WeatherBucket.SNOWY
        low_sun = (
            sun_elevation is not None
            and LOW_SUN_MIN_ELEVATION < sun_elevation <= LOW_SUN_MAX_ELEVATION
        )
        heavy_frost = (
            frost_detected == "heavy_frost"
            and frost_score is not None
            and (frost_score >= 80)
        )
        if heavy_frost:
            return WeatherBucket.FROST_LOW_SUN if low_sun else WeatherBucket.FROST
        if visibility_m is not None and visibility_m < 5000:
            return WeatherBucket.FOG_LOW_SUN if low_sun else WeatherBucket.FOG
        if precipitation_mm is not None and precipitation_mm > BUCKET_RAIN_THRESHOLD:
            snowing = temperature_c is not None and temperature_c < 2.0
            if snowing:
                return (
                    WeatherBucket.SNOWING_LOW_SUN if low_sun else WeatherBucket.SNOWING
                )
            return WeatherBucket.RAINY_LOW_SUN if low_sun else WeatherBucket.RAINY
        if cloud_cover <= BUCKET_CLEAR_MAX:
            weather_bucket = (
                WeatherBucket.CLEAR_LOW_SUN if low_sun else WeatherBucket.CLEAR
            )
        elif cloud_cover <= BUCKET_FAIR_MAX:
            weather_bucket = (
                WeatherBucket.FAIR_LOW_SUN if low_sun else WeatherBucket.FAIR
            )
        elif cloud_cover <= BUCKET_CLOUDY_MAX:
            weather_bucket = (
                WeatherBucket.CLOUDY_LOW_SUN if low_sun else WeatherBucket.CLOUDY
            )
        else:
            weather_bucket = WeatherBucket.OVERCAST
        return weather_bucket if True else None

    def _clamp_raw_bucket(
        self, value: float, global_factor: float, bucket: str, **kwargs
    ) -> float:
        """Raw-contract bucket band: absolute floor, union ceiling, weather cap."""
        clamped_value = clamp_union_hourly(
            value, global_factor, MIN_CORRECTION_FACTOR, MAX_CORRECTION_FACTOR
        )
        unused_lower_limit, upper_limit = self._get_bucket_factor_limits(
            bucket, **kwargs
        )
        return min(upper_limit, clamped_value)

    def _read_bucket_factor(
        self, factors: GroupCalibrationFactors, bucket: str, value: float, **kwargs
    ) -> float:
        if is_raw_forecast_contract(factors.learning_contract_version):
            return self._clamp_raw_bucket(
                value, factors.global_factor, bucket, **kwargs
            )
        return self._clamp_bucket_factor(bucket, value, **kwargs)

    def _read_hourly_factor(
        self, factors: GroupCalibrationFactors, value: float
    ) -> float:
        if is_raw_forecast_contract(factors.learning_contract_version):
            return clamp_union_hourly(
                value,
                factors.global_factor,
                MIN_CORRECTION_FACTOR,
                MAX_CORRECTION_FACTOR,
            )
        return value

    def get_correction_factor(
        self,
        group_name: str,
        hour: Optional[int] = None,
        cloud_cover: Optional[float] = None,
        sun_elevation: Optional[float] = None,
        visibility_m: Optional[float] = None,
        precipitation_mm: Optional[float] = None,
        temperature_c: Optional[float] = None,
        snow_covered_panels: bool = False,
        frost_detected: Optional[str] = None,
        frost_score: Optional[int] = None,
        ghi_ratio: Optional[float] = None,
    ) -> float:
        """Get correction factor for a group, hour, and weather condition. @zara"""
        if group_name not in self._factors:
            return 1.0
        group_factors = self._factors[group_name]
        if is_raw_forecast_contract(group_factors.learning_contract_version):
            if (
                group_factors.rearm_learning_days is not None
                and group_factors.rearm_learning_days < RAW_LEARN_DAYS
            ):
                return 1.0
        elif (
            group_factors.rearm_learning_days is not None
            and group_factors.rearm_learning_days < TILT_REARM_MIN_DAYS
        ):
            return 1.0
        if should_hold_post_cap_rearm(
            group_factors.learning_contract_version,
            group_factors.sample_count,
            MIN_SAMPLES_FOR_FULL_CONFIDENCE,
        ):
            return 1.0
        if (
            cloud_cover is not None
            or visibility_m is not None
            or precipitation_mm is not None
            or snow_covered_panels
            or frost_detected
            or frost_score
        ):
            weather_bucket = self._classify_weather(
                cloud_cover if cloud_cover is not None else 50.0,
                sun_elevation,
                hour,
                precipitation_mm,
                snow_covered_panels,
                visibility_m,
                temperature_c,
                frost_detected,
                frost_score,
            )
            bucket_factors = group_factors.bucket_factors.get(weather_bucket.value)
            if (
                bucket_factors
                and hour is not None
                and (hour in bucket_factors.hourly_factors)
            ):
                return self._read_bucket_factor(
                    group_factors,
                    weather_bucket.value,
                    bucket_factors.hourly_factors[hour],
                    cloud_cover=cloud_cover,
                    sun_elevation=sun_elevation,
                    hour=hour,
                )
            if weather_bucket in (
                WeatherBucket.CLEAR_LOW_SUN,
                WeatherBucket.FAIR_LOW_SUN,
            ):
                base_bucket = (
                    WeatherBucket.CLEAR
                    if weather_bucket == WeatherBucket.CLEAR_LOW_SUN
                    else WeatherBucket.FAIR
                )
                base_bucket_factors = group_factors.bucket_factors.get(
                    base_bucket.value
                )
                if (
                    base_bucket_factors
                    and base_bucket_factors.sample_count >= MIN_SAMPLES_FOR_FACTOR
                ):
                    return self._read_bucket_factor(
                        group_factors,
                        base_bucket.value,
                        base_bucket_factors.global_factor,
                    )
            if weather_bucket == WeatherBucket.CLOUDY_LOW_SUN:
                cloudy_factors = group_factors.bucket_factors.get(
                    WeatherBucket.CLOUDY.value
                )
                if (
                    cloudy_factors
                    and cloudy_factors.sample_count >= MIN_SAMPLES_FOR_FACTOR
                ):
                    return self._read_bucket_factor(
                        group_factors,
                        WeatherBucket.CLOUDY.value,
                        cloudy_factors.global_factor,
                    )
            if weather_bucket in (WeatherBucket.FOG, WeatherBucket.FOG_LOW_SUN):
                overcast_factors = group_factors.bucket_factors.get(
                    WeatherBucket.OVERCAST.value
                )
                if (
                    overcast_factors
                    and overcast_factors.sample_count >= MIN_SAMPLES_FOR_FACTOR
                ):
                    return self._read_bucket_factor(
                        group_factors,
                        WeatherBucket.OVERCAST.value,
                        overcast_factors.global_factor,
                        cloud_cover=cloud_cover,
                        sun_elevation=sun_elevation,
                        hour=hour,
                    )
            if weather_bucket in (WeatherBucket.SNOWING, WeatherBucket.SNOWING_LOW_SUN):
                rainy_factors = group_factors.bucket_factors.get(
                    WeatherBucket.RAINY.value
                )
                if (
                    rainy_factors
                    and rainy_factors.sample_count >= MIN_SAMPLES_FOR_FACTOR
                ):
                    return self._read_bucket_factor(
                        group_factors,
                        WeatherBucket.RAINY.value,
                        rainy_factors.global_factor,
                        cloud_cover=cloud_cover,
                        sun_elevation=sun_elevation,
                        hour=hour,
                    )
            if weather_bucket == WeatherBucket.RAINY_LOW_SUN:
                rainy_factors = group_factors.bucket_factors.get(
                    WeatherBucket.RAINY.value
                )
                if (
                    rainy_factors
                    and rainy_factors.sample_count >= MIN_SAMPLES_FOR_FACTOR
                ):
                    return self._read_bucket_factor(
                        group_factors,
                        WeatherBucket.RAINY.value,
                        rainy_factors.global_factor,
                        cloud_cover=cloud_cover,
                        sun_elevation=sun_elevation,
                        hour=hour,
                    )
            if weather_bucket == WeatherBucket.SNOWY:
                rainy_factors = group_factors.bucket_factors.get(
                    WeatherBucket.RAINY.value
                )
                if (
                    rainy_factors
                    and rainy_factors.sample_count >= MIN_SAMPLES_FOR_FACTOR
                ):
                    return self._read_bucket_factor(
                        group_factors,
                        WeatherBucket.RAINY.value,
                        rainy_factors.global_factor,
                        cloud_cover=cloud_cover,
                        sun_elevation=sun_elevation,
                        hour=hour,
                    )
            if weather_bucket in (WeatherBucket.FROST, WeatherBucket.FROST_LOW_SUN):
                cloudy_factors = group_factors.bucket_factors.get(
                    WeatherBucket.CLOUDY.value
                )
                if (
                    cloudy_factors
                    and cloudy_factors.sample_count >= MIN_SAMPLES_FOR_FACTOR
                ):
                    return self._read_bucket_factor(
                        group_factors,
                        WeatherBucket.CLOUDY.value,
                        cloudy_factors.global_factor,
                    )
                overcast_factors = group_factors.bucket_factors.get(
                    WeatherBucket.OVERCAST.value
                )
                if (
                    overcast_factors
                    and overcast_factors.sample_count >= MIN_SAMPLES_FOR_FACTOR
                ):
                    return self._read_bucket_factor(
                        group_factors,
                        WeatherBucket.OVERCAST.value,
                        overcast_factors.global_factor,
                        cloud_cover=cloud_cover,
                        sun_elevation=sun_elevation,
                        hour=hour,
                    )
        if hour is not None and hour in group_factors.hourly_factors:
            return self._read_hourly_factor(
                group_factors, group_factors.hourly_factors[hour]
            )
        return self._read_hourly_factor(group_factors, group_factors.global_factor)

    def get_all_factors(self) -> Dict[str, GroupCalibrationFactors]:
        """Get all calibration factors. @zara"""
        return self._factors.copy()

    async def learn_from_day(self, date: str) -> Dict[str, Any]:
        "Learn calibration factors from a day's production data. @zara\n\nLoads hourly prediction data from DB and delegates to calibrate_from_daily_data().\nCalled by production_scheduled_tasks._run_physics_calibration().\n\nArgs:\n    date: ISO date string (YYYY-MM-DD)\n\nReturns:\n    Dict with success status and metrics\n"
        try:
            rows = await self._db.fetchall(
                "SELECT ppg.group_name, hp.target_hour,\n                          ppg.physics_kwh, ppg.actual_kwh,\n                          wf.clouds, ac.sun_elevation_deg,\n                          wf.visibility_m, wf.rain,\n                          wf.temperature,\n                          COALESCE(ppg.snow_covered_group, hwa.snow_covered_panels) AS snow_covered,\n                          hwa.frost_detected, hwa.frost_score,\n                          acpg.theoretical_kwh,\n                          ac.clear_sky_radiation_wm2,\n                          hwa.solar_radiation_wm2,\n                          hwa.cloud_cover_percent,\n                          hp.prediction_created_at,\n                          ac.version,\n                          ac.sun_azimuth_deg,\n                          COALESCE(pgeg_at.tilt, pgeg_active.tilt),\n                          COALESCE(pgeg_at.azimuth, pgeg_active.azimuth),\n                          COALESCE(ppg.power_wp_at_prediction,\n                                   pgeg_at.power_wp, pgeg_active.power_wp),\n                          ppg.raw_physics_kwh,\n                          wf.direct_radiation,\n                          wf.diffuse_radiation\n                   FROM prediction_panel_groups ppg\n                   JOIN hourly_predictions hp\n                       ON ppg.prediction_id = hp.prediction_id\n                   LEFT JOIN weather_forecast wf\n                       ON hp.target_date = wf.forecast_date AND hp.target_hour = wf.hour\n                   LEFT JOIN astronomy_cache ac\n                       ON hp.target_date = ac.cache_date AND hp.target_hour = ac.hour\n                   LEFT JOIN hourly_weather_actual hwa\n                       ON hp.target_date = hwa.date AND hp.target_hour = hwa.hour\n                   LEFT JOIN astronomy_cache_panel_groups acpg\n                       ON hp.target_date = acpg.cache_date\n                       AND hp.target_hour = acpg.hour\n                       AND ppg.group_name = acpg.group_name\n                   LEFT JOIN panel_group_config_epochs pge_active\n                       ON pge_active.valid_to IS NULL\n                   LEFT JOIN panel_group_config_epoch_groups pgeg_active\n                       ON pgeg_active.epoch_id = pge_active.epoch_id\n                      AND pgeg_active.display_name = ppg.group_name\n                   LEFT JOIN panel_group_config_epoch_groups pgeg_at\n                       ON pgeg_at.epoch_id = ppg.config_epoch_id\n                      AND pgeg_at.display_name = ppg.group_name\n                   WHERE hp.target_date = ?\n                       AND ppg.actual_kwh IS NOT NULL\n                       AND ppg.physics_kwh IS NOT NULL\n                       AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n                       AND (hp.mppt_throttled = FALSE OR hp.mppt_throttled IS NULL)\n                       AND (ppg.exclude_from_learning_group IS NULL OR ppg.exclude_from_learning_group = 0)\n                       AND (ppg.topology_compatible = TRUE OR ppg.topology_compatible IS NULL)\n                       AND (\n                           (ppg.config_epoch_id IS NOT NULL\n                            AND ppg.group_lineage_uid = pgeg_active.group_lineage_uid)\n                           OR\n                           (ppg.config_epoch_id IS NULL\n                            AND ppg.actual_kwh <= MAX(COALESCE(pgeg_active.power_wp, 0) / 1000.0 * 1.3, 0.5))\n                       )",
                (date,),
            )
            if not rows:
                _LOGGER.debug("Physics learning: No data for %s", date)
                return {"success": False, "groups_updated": 0, "total_samples": 0}
            samples_by_group = {}
            for row in rows:
                group_name = row[0]
                if group_name not in samples_by_group:
                    samples_by_group[group_name] = []
                samples_by_group[group_name].append(
                    {
                        "hour": row[1],
                        "physics_kwh": row[2],
                        "actual_kwh": row[3],
                        "cloud_cover": row[4],
                        "sun_elevation": row[5],
                        "visibility_m": row[6],
                        "precipitation_mm": row[7],
                        "temperature_c": row[8],
                        "snow_covered_panels": bool(row[9]) if row[9] else False,
                        "frost_detected": "heavy_frost" if row[10] else None,
                        "frost_score": row[11],
                        "theoretical_kwh": row[12],
                        "clear_sky_ghi": row[13],
                        "actual_ghi": row[14],
                        "actual_cloud_cover": row[15],
                        "prediction_created_at": row[16],
                        "astronomy_formula_version": row[17],
                        "sun_azimuth": row[18] if len(row) > 18 else None,
                        "panel_tilt_deg": row[19] if len(row) > 19 else None,
                        "panel_azimuth_deg": row[20] if len(row) > 20 else None,
                        "panel_power_wp": row[21] if len(row) > 21 else None,
                        "raw_physics_kwh": row[22] if len(row) > 22 else None,
                        "direct_radiation": row[23] if len(row) > 23 else None,
                        "diffuse_radiation": row[24] if len(row) > 24 else None,
                        "date": date,
                    }
                )
                continue
            result = await self.calibrate_from_daily_data(date, samples_by_group)
            return {
                "success": result.success,
                "groups_updated": result.groups_calibrated,
                "total_samples": result.total_samples,
            }
        except Exception as error:
            _LOGGER.error("Physics learning failed for %s: %s", date, error)
            return {"success": False, "groups_updated": 0, "total_samples": 0}

    async def calibrate_from_daily_data(
        self, date: str, group_data: Dict[str, List[Dict[str, Any]]]
    ) -> CalibrationResult:
        """Perform calibration from daily production data. @zara

        Args:
            date: Date string (YYYY-MM-DD)
            group_data: Dict mapping group_name to list of hourly data

        Returns:
            CalibrationResult with success status and details
        """
        try:
            total_samples = 0
            daily_ratios_by_group = {}
            hourly_ratios_by_group = {}
            bucket_ratios_by_group = {}
            bucket_hourly_ratios_by_group = {}
            sample_ledger = []
            clamp_hits = []
            for group_name, hourly_samples in group_data.items():
                daily_ratios_by_group[group_name] = []
                hourly_ratios_by_group[group_name] = {}
                bucket_ratios_by_group[group_name] = {
                    bucket_kind.value: [] for bucket_kind in WeatherBucket
                }
                bucket_hourly_ratios_by_group[group_name] = {
                    bucket_kind.value: {} for bucket_kind in WeatherBucket
                }
                group_factors = self._factors.get(group_name)
                if group_factors is None:
                    group_factors = GroupCalibrationFactors(
                        group_name=group_name,
                        learning_contract_version=PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT,
                        rearm_learning_days=0,
                        poa_contract_stamped_at=(
                            SafeDateTimeUtil.now if True else SafeDateTimeUtil.now
                        )().isoformat(),
                    )
                    self._factors[group_name] = group_factors
                elif group_factors.learning_contract_version in (
                    PHYSICS_CALIBRATION_POA_DNI_IAM_CONTRACT,
                    PHYSICS_CALIBRATION_GHI_FORECAST_SCALE_CONTRACT,
                    PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT,
                    PHYSICS_CALIBRATION_RAW_FORECAST_V1_CONTRACT,
                    PHYSICS_CALIBRATION_RAW_FORECAST_V2_CONTRACT,
                    PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT,
                ) and (not group_factors.poa_contract_stamped_at):
                    group_factors.poa_contract_stamped_at = (
                        SafeDateTimeUtil.now if True else SafeDateTimeUtil.now
                    )().isoformat()
                for sample in hourly_samples:
                    sample.setdefault("date", date)
                    if not poa_contract_hour_is_learnable(
                        group_factors.learning_contract_version,
                        group_factors.poa_contract_stamped_at,
                        sample.get("prediction_created_at"),
                    ):
                        continue
                    hour = sample.get("hour", 0)
                    physics_kwh = sample.get("physics_kwh", 0)
                    actual_kwh = sample.get("actual_kwh", 0)
                    cloud_cover = sample.get("actual_cloud_cover") or sample.get(
                        "cloud_cover"
                    )
                    sun_elevation = sample.get("sun_elevation")
                    visibility_m = sample.get("visibility_m")
                    precipitation_mm = sample.get("precipitation_mm")
                    temperature_c = sample.get("temperature_c")
                    snow_covered = sample.get("snow_covered_panels", False)
                    frost_detected = sample.get("frost_detected")
                    frost_score = sample.get("frost_score")
                    morning_hour = 7 <= hour <= 10
                    minimum_kwh = (
                        MIN_PRODUCTION_KWH_MORNING
                        if morning_hour
                        else MIN_PRODUCTION_KWH
                    )
                    reference_kwh = physics_kwh
                    reference_kind = PHYSICS_REF_KIND_FORECAST
                    if is_raw_forecast_contract(
                        group_factors.learning_contract_version
                    ):
                        raw_physics_value = sample.get("raw_physics_kwh")
                        if raw_physics_value is None:
                            continue
                        try:
                            reference_kwh = float(raw_physics_value)
                        except (TypeError, ValueError):
                            continue
                        if not (math.isfinite if True else math.isfinite)(
                            reference_kwh
                        ):
                            continue
                    if reference_kwh < minimum_kwh or actual_kwh < minimum_kwh:
                        continue
                    unclamped_ratio = actual_kwh / reference_kwh
                    ratio = unclamped_ratio
                    ratio_clamp_hit = 0
                    if ratio < MIN_CORRECTION_FACTOR or ratio > MAX_CORRECTION_FACTOR:
                        _LOGGER.debug(
                            f"Clamping outlier: {group_name} hour {hour}, ratio={ratio:.2f} -> {max(MIN_CORRECTION_FACTOR, min(MAX_CORRECTION_FACTOR, ratio)):.2f}"
                        )
                        ratio = max(
                            MIN_CORRECTION_FACTOR, min(MAX_CORRECTION_FACTOR, ratio)
                        )
                        ratio_clamp_hit = 1
                        clamp_hits.append(
                            {
                                "date": date,
                                "group_name": group_name,
                                "hour": hour,
                                "bucket_name": None,
                                "clamp_kind": "ratio",
                                "unclamped_value": unclamped_ratio,
                                "clamped_value": ratio,
                            }
                        )
                    learning_disposition = "all"
                    if (
                        is_raw_forecast_contract(
                            group_factors.learning_contract_version
                        )
                        and self._sun_shadow is not None
                    ):
                        learning_disposition = self._sun_shadow.learning_disposition(
                            group=group_name,
                            day=str(date),
                            hour=int(hour),
                            azimuth=sample.get("sun_azimuth"),
                            elevation=sun_elevation,
                            actual=actual_kwh,
                            raw=reference_kwh,
                            global_factor=group_factors.global_factor,
                            measured_ghi=sample.get("actual_ghi"),
                            clearsky_ghi=sample.get("clear_sky_ghi"),
                            direct=sample.get("direct_radiation"),
                            diffuse=sample.get("diffuse_radiation"),
                            clouds=cloud_cover,
                            snow=bool(snow_covered),
                            frost=frost_detected,
                            exclude=bool(sample.get("exclude_from_learning")),
                            mppt=bool(sample.get("mppt_throttled")),
                            exclude_group=bool(
                                sample.get("exclude_from_learning_group")
                            ),
                        )
                    if learning_disposition == "skip":
                        continue
                    if learning_disposition != "hourly":
                        daily_ratios_by_group[group_name].append(ratio)
                    if hour not in hourly_ratios_by_group[group_name]:
                        hourly_ratios_by_group[group_name][hour] = []
                    hourly_ratios_by_group[group_name][hour].append(ratio)
                    bucket_name = None
                    if (
                        cloud_cover is not None
                        or snow_covered
                        or precipitation_mm
                        or (visibility_m is not None)
                        or (frost_detected and frost_detected not in ("none", False))
                        or frost_score
                    ):
                        weather_bucket = self._classify_weather(
                            cloud_cover if cloud_cover is not None else 50.0,
                            sun_elevation,
                            hour,
                            precipitation_mm,
                            snow_covered,
                            visibility_m,
                            temperature_c,
                            frost_detected,
                            frost_score,
                        )
                        bucket_name = weather_bucket.value
                        bucket_ratios_by_group[group_name][weather_bucket.value].append(
                            ratio
                        )
                        if (
                            hour
                            not in bucket_hourly_ratios_by_group[group_name][
                                weather_bucket.value
                            ]
                        ):
                            bucket_hourly_ratios_by_group[group_name][
                                weather_bucket.value
                            ][hour] = []
                        bucket_hourly_ratios_by_group[group_name][weather_bucket.value][
                            hour
                        ].append(ratio)
                    sample_ledger.append(
                        {
                            "date": date,
                            "group_name": group_name,
                            "hour": hour,
                            "physics_ref_kind": reference_kind,
                            "physics_ref_kwh": reference_kwh,
                            "actual_kwh": actual_kwh,
                            "unclamped_ratio": unclamped_ratio,
                            "clamped_ratio": ratio,
                            "ratio_clamp_hit": ratio_clamp_hit,
                            "bucket_name": bucket_name,
                        }
                    )
                    total_samples += 1
                    continue
            history_groups = {}
            for group_name in daily_ratios_by_group:
                if not daily_ratios_by_group[group_name] and (
                    not hourly_ratios_by_group[group_name]
                ):
                    continue
                if group_name not in self._factors:
                    self._factors[group_name] = GroupCalibrationFactors(
                        group_name=group_name,
                        learning_contract_version=PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT,
                        rearm_learning_days=0,
                        poa_contract_stamped_at=(
                            SafeDateTimeUtil.now if True else SafeDateTimeUtil.now
                        )().isoformat(),
                    )
                group_factors = self._factors[group_name]
                if is_raw_forecast_contract(group_factors.learning_contract_version):
                    history_groups[group_name] = self._update_raw_damp(
                        group_factors,
                        daily_ratios_by_group[group_name],
                        hourly_ratios_by_group[group_name],
                        bucket_ratios_by_group[group_name],
                        bucket_hourly_ratios_by_group[group_name],
                    )
                    continue
                history_groups[group_name] = {
                    "hourly": {},
                    "sample_count": 0,
                    "buckets": {},
                }
                daily_mean_ratio = sum(daily_ratios_by_group[group_name]) / len(
                    daily_ratios_by_group[group_name]
                )
                if group_factors.rearm_learning_days is not None:
                    if group_factors.rearm_learning_days < TILT_REARM_MIN_DAYS:
                        if group_factors.rearm_learning_days < 1:
                            group_factors.rearm_learning_days = 1
                            group_factors.rearm_pending_factor = daily_mean_ratio
                            group_factors.global_factor = 1.0
                            continue
                        pending_factor = group_factors.rearm_pending_factor
                        group_factors.global_factor = (
                            1.0 if pending_factor is None else pending_factor
                        )
                        if group_factors.sample_count < EMA_MIN_SAMPLES_FOR_EMA:
                            group_factors.sample_count = EMA_MIN_SAMPLES_FOR_EMA
                        group_factors.rearm_learning_days = None
                        group_factors.rearm_pending_factor = None
                alpha = self._adaptive_alpha(daily_mean_ratio)
                if group_factors.sample_count >= EMA_MIN_SAMPLES_FOR_EMA:
                    proposed_global_factor = (
                        alpha * daily_mean_ratio
                        + (1 - alpha) * group_factors.global_factor
                    )
                else:
                    proposed_global_factor = daily_mean_ratio
                clamped_global_factor = max(
                    MIN_CORRECTION_FACTOR,
                    min(MAX_CORRECTION_FACTOR, proposed_global_factor),
                )
                if clamped_global_factor != proposed_global_factor:
                    clamp_hits.append(
                        {
                            "date": date,
                            "group_name": group_name,
                            "hour": None,
                            "bucket_name": None,
                            "clamp_kind": "global_factor",
                            "unclamped_value": proposed_global_factor,
                            "clamped_value": clamped_global_factor,
                        }
                    )
                group_factors.global_factor = clamped_global_factor
                group_factors.sample_count += len(daily_ratios_by_group[group_name])
                for hour, ratios in hourly_ratios_by_group[group_name].items():
                    if not ratios:
                        continue
                    hourly_mean_ratio = sum(ratios) / len(ratios)
                    if hour in group_factors.hourly_factors:
                        proposed_hourly_factor = (
                            alpha * hourly_mean_ratio
                            + (1 - alpha) * group_factors.hourly_factors[hour]
                        )
                    else:
                        proposed_hourly_factor = hourly_mean_ratio
                    clamped_hourly_factor = max(
                        MIN_CORRECTION_FACTOR,
                        min(MAX_CORRECTION_FACTOR, proposed_hourly_factor),
                    )
                    if clamped_hourly_factor != proposed_hourly_factor:
                        clamp_hits.append(
                            {
                                "date": date,
                                "group_name": group_name,
                                "hour": hour,
                                "bucket_name": None,
                                "clamp_kind": "hourly_factor",
                                "unclamped_value": proposed_hourly_factor,
                                "clamped_value": clamped_hourly_factor,
                            }
                        )
                    group_factors.hourly_factors[hour] = clamped_hourly_factor
                    history_groups[group_name]["hourly"][str(hour)] = hourly_mean_ratio
                    history_groups[group_name]["sample_count"] += len(ratios)
                    continue
                for bucket_key, ratios in bucket_ratios_by_group[group_name].items():
                    if not ratios:
                        continue
                    if bucket_key not in group_factors.bucket_factors:
                        group_factors.bucket_factors[bucket_key] = BucketFactors()
                    bucket_factor = group_factors.bucket_factors[bucket_key]
                    bucket_mean_ratio = sum(ratios) / len(ratios)
                    if bucket_factor.sample_count >= EMA_MIN_SAMPLES_FOR_EMA:
                        proposed_bucket_factor = (
                            alpha * bucket_mean_ratio
                            + (1 - alpha) * bucket_factor.global_factor
                        )
                    else:
                        proposed_bucket_factor = bucket_mean_ratio
                    clamped_bucket_factor = self._clamp_bucket_factor(
                        bucket_key, proposed_bucket_factor
                    )
                    if clamped_bucket_factor != proposed_bucket_factor:
                        clamp_hits.append(
                            {
                                "date": date,
                                "group_name": group_name,
                                "hour": None,
                                "bucket_name": bucket_key,
                                "clamp_kind": "bucket_global",
                                "unclamped_value": proposed_bucket_factor,
                                "clamped_value": clamped_bucket_factor,
                            }
                        )
                    bucket_factor.global_factor = clamped_bucket_factor
                    bucket_factor.sample_count += len(ratios)
                    bucket_factor.confidence = min(
                        1.0,
                        bucket_factor.sample_count / MIN_SAMPLES_FOR_FULL_CONFIDENCE,
                    )
                    history_groups[group_name]["buckets"][bucket_key] = {
                        "hourly": {},
                        "sample_count": len(ratios),
                    }
                    for hour, bucket_hourly_ratios in bucket_hourly_ratios_by_group[
                        group_name
                    ][bucket_key].items():
                        if not bucket_hourly_ratios:
                            continue
                        bucket_hourly_mean_ratio = sum(bucket_hourly_ratios) / len(
                            bucket_hourly_ratios
                        )
                        if hour in bucket_factor.hourly_factors:
                            proposed_bucket_hourly_factor = (
                                alpha * bucket_hourly_mean_ratio
                                + (1 - alpha) * bucket_factor.hourly_factors[hour]
                            )
                        else:
                            proposed_bucket_hourly_factor = bucket_hourly_mean_ratio
                        clamped_bucket_hourly_factor = self._clamp_bucket_factor(
                            bucket_key, proposed_bucket_hourly_factor
                        )
                        if (
                            clamped_bucket_hourly_factor
                            != proposed_bucket_hourly_factor
                        ):
                            clamp_hits.append(
                                {
                                    "date": date,
                                    "group_name": group_name,
                                    "hour": hour,
                                    "bucket_name": bucket_key,
                                    "clamp_kind": "bucket_hourly",
                                    "unclamped_value": proposed_bucket_hourly_factor,
                                    "clamped_value": clamped_bucket_hourly_factor,
                                }
                            )
                        bucket_factor.hourly_factors[hour] = (
                            clamped_bucket_hourly_factor
                        )
                        history_groups[group_name]["buckets"][bucket_key]["hourly"][
                            str(hour)
                        ] = bucket_hourly_mean_ratio
                    continue
                group_factors.confidence = min(
                    1.0,
                    group_factors.sample_count
                    / (
                        MIN_SAMPLES_FOR_FULL_CONFIDENCE
                        * len(group_factors.hourly_factors)
                        if group_factors.hourly_factors
                        else MIN_SAMPLES_FOR_FULL_CONFIDENCE
                    ),
                )
                group_factors.last_updated = (
                    SafeDateTimeUtil.now if True else SafeDateTimeUtil.now
                )().isoformat()
                continue
            await self._save_config()
            await self._persist_sun_shadow()
            await self._save_history(date, history_groups)
            await self._save_calibration_telemetry(date, sample_ledger, clamp_hits)
            average_factor = 1.0
            if self._factors:
                average_factor = sum(
                    (factor_set.global_factor for factor_set in self._factors.values())
                ) / len(self._factors)
            return CalibrationResult(
                success=True,
                message=f"Calibrated {len(daily_ratios_by_group)} groups with {total_samples} samples",
                groups_calibrated=len(daily_ratios_by_group),
                total_samples=total_samples,
                avg_correction_factor=round(average_factor, 3),
            )
        except Exception as error:
            _LOGGER.error(f"Calibration failed: {error}", exc_info=True)
            return CalibrationResult(success=False, message=str(error))

    async def recalculate_factors(self) -> None:
        """Recalculate all factors from history. @zara"""
        try:
            retained_factors = {
                factor_group_name: group_factors
                for factor_group_name, group_factors in self._factors.items()
                if not should_rebuild_factors_from_history(
                    group_factors.learning_contract_version
                )
            }
            self._factors = {}
            await self._load_history()
            for history_day in self._history:
                history_groups = history_day.get("groups", {})
                for group_name, group_history in history_groups.items():
                    if group_name in retained_factors:
                        continue
                    if group_name not in self._factors:
                        self._factors[group_name] = GroupCalibrationFactors(
                            group_name=group_name,
                            learning_contract_version=PHYSICS_CALIBRATION_SENSOR_GHI_CONTRACT,
                        )
                    group_factors = self._factors[group_name]
                    for hour_key, value in group_history.get("hourly", {}).items():
                        hour = int(hour_key)
                        alpha = self._adaptive_alpha(value)
                        if hour not in group_factors.hourly_factors:
                            group_factors.hourly_factors[hour] = value
                        else:
                            group_factors.hourly_factors[hour] = (
                                alpha * value
                                + (1 - alpha) * group_factors.hourly_factors[hour]
                            )
                        group_factors.sample_count += 1
                    for bucket_key, bucket_values in group_history.get(
                        "buckets", {}
                    ).items():
                        if bucket_key not in group_factors.bucket_factors:
                            group_factors.bucket_factors[bucket_key] = BucketFactors()
                        bucket = group_factors.bucket_factors[bucket_key]
                        for hour_key, value in bucket_values.get("hourly", {}).items():
                            hour = int(hour_key)
                            alpha = self._adaptive_alpha(value)
                            if hour not in bucket.hourly_factors:
                                bucket.hourly_factors[hour] = value
                            else:
                                bucket.hourly_factors[hour] = (
                                    alpha * value
                                    + (1 - alpha) * bucket.hourly_factors[hour]
                                )
                            bucket.hourly_factors[hour] = self._clamp_bucket_factor(
                                bucket_key, bucket.hourly_factors[hour]
                            )
                            bucket.sample_count += 1
                        if bucket.hourly_factors:
                            number = len(bucket.hourly_factors) or 1
                            bucket.global_factor = self._clamp_bucket_factor(
                                bucket_key, sum(bucket.hourly_factors.values()) / number
                            )
                        bucket.confidence = min(
                            1.0, bucket.sample_count / MIN_SAMPLES_FOR_FULL_CONFIDENCE
                        )
                    if (
                        group_factors.hourly_factors
                        and group_factors.global_factor == 1.0
                    ):
                        group_factors.global_factor = sum(
                            group_factors.hourly_factors.values()
                        ) / len(group_factors.hourly_factors)
                    group_factors.confidence = min(
                        1.0,
                        group_factors.sample_count
                        / (
                            MIN_SAMPLES_FOR_FULL_CONFIDENCE
                            * len(group_factors.hourly_factors)
                            if group_factors.hourly_factors
                            else MIN_SAMPLES_FOR_FULL_CONFIDENCE
                        ),
                    )
                    group_factors.last_updated = (
                        SafeDateTimeUtil.now if True else SafeDateTimeUtil.now
                    )().isoformat()
                continue
            self._factors.update(retained_factors)
            await self._save_config()
            _LOGGER.info("Recalculated calibration factors from history")
        except Exception as error:
            _LOGGER.error(f"Factor recalculation failed: {error}")
        if True:
            return

    def get_calibration_summary(self) -> Dict[str, Any]:
        """Get summary of current calibration state. @zara"""
        summary = {
            "groups": {},
            "total_groups": len(self._factors),
            "total_bucket_factors": 0,
        }
        for group_name, group_factors in self._factors.items():
            bucket_count = len(group_factors.bucket_factors)
            summary["total_bucket_factors"] += bucket_count
            summary["groups"][group_name] = {
                "global_factor": round(group_factors.global_factor, 3),
                "hourly_factors_count": len(group_factors.hourly_factors),
                "bucket_factors_count": bucket_count,
                "sample_count": group_factors.sample_count,
                "confidence": round(group_factors.confidence, 2),
                "last_updated": group_factors.last_updated,
            }
        return summary

    def _empty_raw_pending(self) -> Dict[str, Any]:
        return {"g": [], "h": {}, "b": {}, "bh": {}}

    def _parse_raw_pending(self, raw: Any) -> Dict[str, Any]:
        pending = self._empty_raw_pending()
        if not raw:
            return pending
        try:
            parsed_data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return pending
        pending["g"] = [
            float(pending_ratio) for pending_ratio in parsed_data.get("g") or []
        ]
        pending["h"] = {
            str(key): [float(pending_ratio) for pending_ratio in pending_ratios]
            for key, pending_ratios in (parsed_data.get("h") or {}).items()
        }
        pending["b"] = {
            str(key): [float(pending_ratio) for pending_ratio in pending_ratios]
            for key, pending_ratios in (parsed_data.get("b") or {}).items()
        }
        for bucket_key, bucket_hours in (parsed_data.get("bh") or {}).items():
            pending["bh"][str(bucket_key)] = {
                str(hour): [float(pending_ratio) for pending_ratio in pending_ratios]
                for hour, pending_ratios in (bucket_hours or {}).items()
            }
        return pending

    def _dump_raw_pending(self, pending: Optional[Dict[str, Any]]) -> Optional[str]:
        if not pending:
            return
        if not (
            pending.get("g")
            or pending.get("h")
            or pending.get("b")
            or pending.get("bh")
        ):
            return
        return json.dumps(pending, separators=(",", ":"), sort_keys=True)

    def _ensure_raw_pending(self, factors: GroupCalibrationFactors) -> Dict[str, Any]:
        pending = factors.raw_damp_pending
        if not isinstance(pending, dict) or "h" not in pending:
            pending = self._empty_raw_pending()
        pending.setdefault("g", [])
        pending.setdefault("h", {})
        pending.setdefault("b", {})
        pending.setdefault("bh", {})
        factors.raw_damp_pending = pending
        return pending

    def _abs_clamp(self, value: float) -> float:
        return max(MIN_CORRECTION_FACTOR, min(MAX_CORRECTION_FACTOR, value))

    def _union_store(self, value: float, global_factor: float) -> float:
        return clamp_union_hourly(
            value, global_factor, MIN_CORRECTION_FACTOR, MAX_CORRECTION_FACTOR
        )

    def _update_raw_damp(
        self,
        factors: GroupCalibrationFactors,
        daily: List[float],
        hourly: Dict[int, List[float]],
        buckets: Dict[str, List[float]],
        bucket_hourly: Dict[str, Dict[int, List[float]]],
    ) -> Dict[str, Any]:
        """Three learning days, then EMA against 1.0, then a 15% step, then the union."""
        history_result = {"hourly": {}, "sample_count": 0, "buckets": {}}
        if not daily and (not hourly) and (not buckets):
            return history_result
        pending = self._ensure_raw_pending(factors)
        if daily:
            daily_mean = sum(daily) / len(daily)
            if factors.rearm_learning_days is None:
                factors.global_factor = ema_raw_factor(
                    factors.global_factor, daily_mean, self._abs_clamp
                )
            else:
                pending["g"] = list(pending.get("g") or []) + [daily_mean]
                learning_days = int(factors.rearm_learning_days or 0) + 1
                if learning_days < RAW_LEARN_DAYS:
                    factors.rearm_learning_days = learning_days
                    factors.global_factor = 1.0
                else:
                    factors.global_factor = publish_raw_factor(
                        pending["g"], self._abs_clamp
                    )
                    pending["g"] = []
                    factors.rearm_learning_days = None
        global_factor = factors.global_factor

        def union_clamp(value: float, bound: float = global_factor) -> float:
            return self._union_store(value, bound)

        for hour, ratios in hourly.items():
            if not ratios:
                continue
            mean_ratio = sum(ratios) / len(ratios)
            hour_key = str(int(hour))
            history_result["hourly"][hour_key] = mean_ratio
            history_result["sample_count"] += len(ratios)
            if int(hour) not in factors.hourly_factors:
                pending_values = list(pending["h"].get(hour_key) or [])
                pending_values.append(mean_ratio)
                if len(pending_values) < RAW_LEARN_DAYS:
                    pending["h"][hour_key] = pending_values
                    continue
                factors.hourly_factors[int(hour)] = publish_raw_factor(
                    pending_values, union_clamp
                )
                pending["h"].pop(hour_key, None)
                continue
            factors.hourly_factors[int(hour)] = ema_raw_factor(
                factors.hourly_factors[int(hour)], mean_ratio, union_clamp
            )
            continue
        for bucket_key, ratios in buckets.items():
            if not ratios:
                continue
            bucket_mean = sum(ratios) / len(ratios)
            history_result["buckets"][bucket_key] = {
                "hourly": {},
                "sample_count": len(ratios),
            }

            def bucket_clamp(
                value: float, bucket: str = bucket_key, bound: float = global_factor
            ) -> float:
                return self._clamp_raw_bucket(value, bound, bucket)

            if bucket_key not in factors.bucket_factors:
                pending_values = list(pending["b"].get(bucket_key) or [])
                pending_values.append(bucket_mean)
                if len(pending_values) < RAW_LEARN_DAYS:
                    pending["b"][bucket_key] = pending_values
                else:
                    factors.bucket_factors[bucket_key] = BucketFactors(
                        global_factor=publish_raw_factor(pending_values, bucket_clamp),
                        sample_count=len(pending_values),
                    )
                    pending["b"].pop(bucket_key, None)
            else:
                bucket_factors = factors.bucket_factors[bucket_key]
                bucket_factors.global_factor = ema_raw_factor(
                    bucket_factors.global_factor, bucket_mean, bucket_clamp
                )
                bucket_factors.sample_count += len(ratios)
                bucket_factors.confidence = min(
                    1.0, bucket_factors.sample_count / MIN_SAMPLES_FOR_FULL_CONFIDENCE
                )
            bucket_hours = bucket_hourly.get(bucket_key) or {}
            pending_bucket_hours = pending["bh"].setdefault(bucket_key, {})
            bucket_factors = factors.bucket_factors.get(bucket_key)
            for hour, bucket_hourly_ratios in bucket_hours.items():
                if not bucket_hourly_ratios:
                    continue
                mean_ratio = sum(bucket_hourly_ratios) / len(bucket_hourly_ratios)
                hour_key = str(int(hour))
                history_result["buckets"][bucket_key]["hourly"][hour_key] = mean_ratio
                if (
                    bucket_factors is None
                    or int(hour) not in bucket_factors.hourly_factors
                ):
                    pending_values = list(pending_bucket_hours.get(hour_key) or [])
                    pending_values.append(mean_ratio)
                    if bucket_factors is None or len(pending_values) < RAW_LEARN_DAYS:
                        pending_bucket_hours[hour_key] = pending_values
                        continue
                    bucket_factors.hourly_factors[int(hour)] = publish_raw_factor(
                        pending_values, bucket_clamp
                    )
                    pending_bucket_hours.pop(hour_key, None)
                    continue
                bucket_factors.hourly_factors[int(hour)] = ema_raw_factor(
                    bucket_factors.hourly_factors[int(hour)], mean_ratio, bucket_clamp
                )
                continue
            if pending_bucket_hours:
                continue
            pending["bh"].pop(bucket_key, None)
            continue
        factors.sample_count += len(daily)
        confidence_denominator = (
            MIN_SAMPLES_FOR_FULL_CONFIDENCE * len(factors.hourly_factors)
            if factors.hourly_factors
            else MIN_SAMPLES_FOR_FULL_CONFIDENCE
        )
        factors.confidence = min(1.0, factors.sample_count / confidence_denominator)
        factors.last_updated = SafeDateTimeUtil.now().isoformat()
        factors.raw_damp_pending = pending
        return history_result

    async def _apply_raw_forecast_contract(self) -> None:
        """Replay raw forecast physics once onto v3, then rescore blend columns.

        v1 and v2 are replayed even when the rescored physics columns would
        match. Shadow positions are built from the same history and written
        in that transaction. A failed write rolls the transaction back. The
        previous contract and its factors stay in place, and the next start
        retries.
        """
        if not self._factors:
            return
        if all(
            (
                not select_raw_forecast_contract(
                    group_factors.learning_contract_version
                )[1]
                for group_factors in self._factors.values()
            )
        ):
            return
        replay_started = time.perf_counter()
        previous_shadow = self._sun_shadow
        self._sun_shadow = SunShadowBook()
        try:
            rows = await self._db.fetchall(self._raw_replay_sql())
            working_factors, replay_status_by_group, blend_scores, contract_stamp = (
                self._replay_raw_contract(rows or [])
            )
            await self._persist_raw_contract(
                working_factors, blend_scores, contract_stamp
            )
        except Exception as error:
            self._sun_shadow = previous_shadow
            _LOGGER.warning(
                "Raw forecast contract migration failed; previous contract kept and the next start will retry: %s",
                error,
            )
            return
        self._factors = working_factors
        for factor_group_name in working_factors:
            group_factors = working_factors[factor_group_name]
            hourly_values = list(group_factors.hourly_factors.values())
            hourly_min = (
                min(hourly_values) if hourly_values else group_factors.global_factor
            )
            hourly_max = (
                max(hourly_values) if hourly_values else group_factors.global_factor
            )
            _LOGGER.info(
                "Physics calibration %s: %s global=%.3f hourly=%d shadow_positions=%d hourly_min=%.3f hourly_max=%.3f",
                factor_group_name,
                replay_status_by_group[factor_group_name],
                group_factors.global_factor,
                len(group_factors.hourly_factors),
                self._sun_shadow.active_count(factor_group_name),
                hourly_min,
                hourly_max,
            )
        _LOGGER.debug(
            "Physics calibration raw contract replay finished in %.1f ms",
            (time.perf_counter() - replay_started) * 1000.0,
        )

    def _raw_replay_sql(self) -> str:
        return "\n            /* raw-forecast-replay */\n            SELECT hp.target_date, ppg.group_name, hp.target_hour,\n                   ppg.raw_physics_kwh, ppg.actual_kwh, ppg.ai_kwh,\n                   wf.clouds, ac.sun_elevation_deg, wf.visibility_m, wf.rain,\n                   wf.temperature, COALESCE(ppg.snow_covered_group, 0),\n                   hwa.frost_detected, hwa.frost_score,\n                   hp.actual_kwh, hp.ai_kwh,\n                   wf.solar_radiation_wm2, wf.direct_radiation, wf.diffuse_radiation,\n                   ac.clear_sky_radiation_wm2, wf.humidity,\n                   hp.prediction_created_at,\n                   ac.sun_azimuth_deg,\n                   hwa.solar_radiation_wm2\n            FROM prediction_panel_groups ppg\n            JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n            LEFT JOIN weather_forecast wf\n                ON hp.target_date = wf.forecast_date AND hp.target_hour = wf.hour\n            LEFT JOIN astronomy_cache ac\n                ON hp.target_date = ac.cache_date AND hp.target_hour = ac.hour\n            LEFT JOIN hourly_weather_actual hwa\n                ON hp.target_date = hwa.date AND hp.target_hour = hwa.hour\n            LEFT JOIN panel_group_config_epochs pge_active\n                ON pge_active.valid_to IS NULL\n            LEFT JOIN panel_group_config_epoch_groups pgeg_active\n                ON pgeg_active.epoch_id = pge_active.epoch_id\n               AND pgeg_active.display_name = ppg.group_name\n            WHERE ppg.raw_physics_kwh IS NOT NULL\n              AND ppg.actual_kwh IS NOT NULL\n              AND (hp.exclude_from_learning = FALSE OR hp.exclude_from_learning IS NULL)\n              AND (hp.mppt_throttled = FALSE OR hp.mppt_throttled IS NULL)\n              AND (ppg.exclude_from_learning_group IS NULL OR ppg.exclude_from_learning_group = 0)\n              AND (ppg.topology_compatible = TRUE OR ppg.topology_compatible IS NULL)\n              AND (\n                  (ppg.config_epoch_id IS NOT NULL\n                   AND ppg.group_lineage_uid = pgeg_active.group_lineage_uid)\n                  OR\n                  (ppg.config_epoch_id IS NULL\n                   AND ppg.actual_kwh <= MAX(COALESCE(pgeg_active.power_wp, 0) / 1000.0 * 1.3, 0.5))\n              )\n            ORDER BY hp.target_date, hp.target_hour, ppg.group_name\n        "

    def _replay_raw_contract(self, rows: List[Any]):
        working_factors = {
            factor_group_name: GroupCalibrationFactors(
                group_name=factor_group_name,
                learning_contract_version=PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT,
                rearm_learning_days=0,
            )
            for factor_group_name in self._factors
        }
        learning_days_by_group = {
            factor_group_name: set() for factor_group_name in working_factors
        }
        rows_by_date = {}
        for row in rows:
            if not row or row[1] not in working_factors:
                continue
            rows_by_date.setdefault(str(row[0]), []).append(row)
        method_folds = []
        group_folds = []
        regime_folds = []
        contract_stamp = None
        regime_classifier = self._regime_classifier()
        previous_factors = self._factors
        self._factors = working_factors
        try:
            for replay_date in sorted(rows_by_date):
                month = int(str(replay_date)[5:7])
                season = get_season(month)
                learning_by_group = {}
                plant_by_hour = {}
                group_errors = {}
                regime_errors = {}
                for row in rows_by_date[replay_date]:
                    replay_sample = self._raw_replay_hour(row)
                    if replay_sample is None:
                        continue
                    contract_stamp = self._later_stamp(
                        contract_stamp, replay_sample["created_at"]
                    )
                    factor_group_name = replay_sample["group"]
                    hour = replay_sample["hour"]
                    replay_factor = working_factors[factor_group_name]
                    disposition = "all"
                    if self._sun_shadow is not None:
                        disposition = self._sun_shadow.learning_disposition(
                            group=factor_group_name,
                            day=str(replay_date),
                            hour=hour,
                            azimuth=replay_sample["azimuth"],
                            elevation=replay_sample["elevation"],
                            actual=replay_sample["actual"],
                            raw=replay_sample["raw"],
                            global_factor=replay_factor.global_factor,
                            measured_ghi=replay_sample["measured_ghi"],
                            clearsky_ghi=replay_sample["clearsky"],
                            direct=replay_sample["direct"],
                            diffuse=replay_sample["diffuse"],
                            clouds=replay_sample["clouds"],
                            snow=replay_sample["snow"],
                            frost=replay_sample["frost"],
                        )
                    if disposition == "skip":
                        correction = (
                            replay_factor.global_factor
                            * self._sun_shadow.factor(
                                factor_group_name,
                                replay_sample["azimuth"],
                                replay_sample["elevation"],
                                replay_sample["direct_share"],
                                before_day=str(replay_date),
                            )
                        )
                    else:
                        correction = self.get_correction_factor(
                            factor_group_name,
                            hour,
                            cloud_cover=replay_sample["clouds"],
                            sun_elevation=replay_sample["elevation"],
                            visibility_m=replay_sample["visibility"],
                            precipitation_mm=replay_sample["rain"],
                            temperature_c=replay_sample["temperature"],
                            snow_covered_panels=replay_sample["snow"],
                            frost_detected=replay_sample["frost"],
                            frost_score=replay_sample["frost_score"],
                        )
                        learning = learning_by_group.setdefault(
                            factor_group_name,
                            {
                                "daily": [],
                                "hourly": {},
                                "buckets": {},
                                "bucket_hourly": {},
                            },
                        )
                        if disposition != "hourly":
                            learning["daily"].append(replay_sample["ratio"])
                        learning["hourly"].setdefault(hour, []).append(
                            replay_sample["ratio"]
                        )
                        bucket = replay_sample["bucket"]
                        if bucket is not None:
                            learning["buckets"].setdefault(bucket, []).append(
                                replay_sample["ratio"]
                            )
                            learning["bucket_hourly"].setdefault(bucket, {}).setdefault(
                                hour, []
                            ).append(replay_sample["ratio"])
                    learning_days_by_group[factor_group_name].add(replay_date)
                    corrected = correction * replay_sample["raw"]
                    cloud = self._cloud_bucket(replay_sample["clouds"])
                    hour_bucket = self._hour_bucket(hour)
                    group_key = (factor_group_name, cloud, hour_bucket)
                    errors = group_errors.setdefault(
                        group_key, {"physics": [], "ai": [], "bias": []}
                    )
                    error = corrected - replay_sample["actual"]
                    errors["physics"].append(abs(error))
                    errors["bias"].append(error)
                    if replay_sample["group_ai"] is not None:
                        errors["ai"].append(
                            abs(replay_sample["group_ai"] - replay_sample["actual"])
                        )
                    plant = plant_by_hour.setdefault(
                        hour,
                        {
                            "physics": 0.0,
                            "actual": replay_sample["plant_actual"],
                            "ai": replay_sample["plant_ai"],
                            "cloud": cloud,
                            "hour_bucket": hour_bucket,
                            "regime": None,
                        },
                    )
                    plant["physics"] += corrected
                    if replay_sample["plant_actual"] is not None:
                        plant["actual"] = replay_sample["plant_actual"]
                    if (
                        regime_classifier is not None
                        and replay_sample["regime_inputs"] is not None
                    ):
                        plant["regime"] = regime_classifier(
                            **replay_sample["regime_inputs"]
                        )
                for factor_group_name, learning_data in learning_by_group.items():
                    self._update_raw_damp(
                        working_factors[factor_group_name],
                        learning_data["daily"],
                        learning_data["hourly"],
                        learning_data["buckets"],
                        learning_data["bucket_hourly"],
                    )
                for hour, plant_data in plant_by_hour.items():
                    if plant_data["actual"] is None:
                        continue
                    score_key = (plant_data["cloud"], plant_data["hour_bucket"])
                    error = plant_data["physics"] - plant_data["actual"]
                    physics_errors = [abs(error)]
                    biases = [error]
                    ai_errors = []
                    if plant_data["ai"] is not None:
                        ai_errors = [abs(plant_data["ai"] - plant_data["actual"])]
                    method_folds.append(
                        (score_key + (None,), physics_errors, ai_errors, biases)
                    )
                    method_folds.append(
                        (score_key + (season,), physics_errors, ai_errors, biases)
                    )
                    if plant_data["regime"]:
                        regime_key = (plant_data["regime"], plant_data["hour_bucket"])
                        regime_errors.setdefault(
                            regime_key, {"physics": [], "ai": [], "bias": []}
                        )
                        regime_errors[regime_key]["physics"].extend(physics_errors)
                        regime_errors[regime_key]["ai"].extend(ai_errors)
                        regime_errors[regime_key]["bias"].extend(biases)
                for score_key, scores in group_errors.items():
                    group_folds.append(
                        (
                            score_key + (None,),
                            scores["physics"],
                            scores["ai"],
                            scores["bias"],
                        )
                    )
                    group_folds.append(
                        (
                            score_key + (season,),
                            scores["physics"],
                            scores["ai"],
                            scores["bias"],
                        )
                    )
                for score_key, scores in regime_errors.items():
                    regime_folds.append(
                        (
                            score_key + (None,),
                            scores["physics"],
                            scores["ai"],
                            scores["bias"],
                        )
                    )
                    regime_folds.append(
                        (
                            score_key + (season,),
                            scores["physics"],
                            scores["ai"],
                            scores["bias"],
                        )
                    )
        finally:
            self._factors = previous_factors
        if contract_stamp is None:
            contract_stamp = (
                SafeDateTimeUtil.now if True else SafeDateTimeUtil.now
            )().isoformat()
        replay_status_by_group = {}
        for factor_group_name, group_factors in working_factors.items():
            if len(learning_days_by_group[factor_group_name]) < RAW_LEARN_DAYS:
                group_factors.global_factor = 1.0
                group_factors.hourly_factors = {}
                group_factors.bucket_factors = {}
                group_factors.raw_damp_pending = {}
                group_factors.rearm_learning_days = 0
                group_factors.sample_count = 0
                group_factors.confidence = 0.0
                replay_status_by_group[factor_group_name] = "B"
            else:
                replay_status_by_group[factor_group_name] = "A"
            group_factors.learning_contract_version = (
                PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT
            )
            group_factors.poa_contract_stamped_at = contract_stamp
        blend_scores = {
            "method": self._rescore_blend(method_folds),
            "group": self._rescore_blend(group_folds),
            "regime": (
                self._rescore_blend(regime_folds)
                if regime_classifier is not None
                else None
            ),
        }
        return (working_factors, replay_status_by_group, blend_scores, contract_stamp)

    def _rescore_blend(self, folds: List[tuple]) -> list:
        try:
            from ..production.production_scheduled_tasks import (
                rescore_raw_forecast_blend as rescore_raw_forecast_blend,
            )
        except Exception:
            return smoothed_mae_rows(folds)
        return rescore_raw_forecast_blend(folds)

    def _regime_classifier(self):
        if hasattr(self, "_regime_classifier_fn"):
            return self._regime_classifier_fn
        try:
            from ..production.production_scheduled_tasks import (
                _classify_static_weather_regime_for_learning as _classify_static_weather_regime_for_learning,
            )
        except Exception as error:
            _LOGGER.warning(
                "Physics calibration regime rescore left unchanged; classifier unavailable: %s",
                error,
            )
            self._regime_classifier_fn = None
            return
        self._regime_classifier_fn = _classify_static_weather_regime_for_learning
        return self._regime_classifier_fn

    def _raw_replay_hour(self, row: Any) -> Optional[Dict[str, Any]]:
        try:
            hour = int(row[2])
            raw_kwh = float(row[3])
            actual_kwh = float(row[4])
        except (TypeError, ValueError, IndexError):
            return
        if (
            not math.isfinite(raw_kwh)
            or not math.isfinite(actual_kwh)
            or raw_kwh <= 0.0
        ):
            return None
        minimum_kwh = (
            MIN_PRODUCTION_KWH_MORNING if 7 <= hour <= 10 else MIN_PRODUCTION_KWH
        )
        if raw_kwh < minimum_kwh or actual_kwh < minimum_kwh:
            return
        ratio = actual_kwh / raw_kwh
        ratio = max(MIN_CORRECTION_FACTOR, min(MAX_CORRECTION_FACTOR, ratio))
        cloud_cover = self._optional_float(row[6])
        bucket_name = None
        if (
            cloud_cover is not None
            or row[11]
            or row[9] is not None
            or (row[8] is not None)
            or row[12]
            or row[13]
        ):
            bucket_name = self._classify_weather(
                cloud_cover if cloud_cover is not None else 50.0,
                self._optional_float(row[7]),
                hour,
                self._optional_float(row[9]),
                bool(row[11]),
                self._optional_float(row[8]),
                self._optional_float(row[10]),
                "heavy_frost" if row[12] else None,
                row[13],
            ).value
        regime_inputs = None
        if self._regime_classifier() is not None:
            regime_inputs = {
                "clouds": 50.0 if cloud_cover is None else cloud_cover,
                "ghi": self._optional_float(row[16]) or 0.0,
                "dni": self._optional_float(row[17]) or 0.0,
                "dhi": self._optional_float(row[18]) or 0.0,
                "clear_sky": self._optional_float(row[19]) or 0.0,
                "rain": self._optional_float(row[9]) or 0.0,
                "humidity": self._optional_float(row[20]) or 0.0,
                "sun_elevation": self._optional_float(row[7]) or 0.0,
                "hour": hour,
            }
        return {
            "group": row[1],
            "hour": hour,
            "raw": raw_kwh,
            "actual": actual_kwh,
            "ratio": ratio,
            "clouds": cloud_cover,
            "elevation": self._optional_float(row[7]),
            "visibility": self._optional_float(row[8]),
            "rain": self._optional_float(row[9]),
            "temperature": self._optional_float(row[10]),
            "snow": bool(row[11]),
            "frost": "heavy_frost" if row[12] else None,
            "frost_score": row[13],
            "plant_actual": self._optional_float(row[14]),
            "plant_ai": self._optional_float(row[15]),
            "group_ai": self._optional_float(row[5]),
            "created_at": row[21] if len(row) > 21 else None,
            "bucket": bucket_name,
            "regime_inputs": regime_inputs,
            "azimuth": self._optional_float(row[22]) if len(row) > 22 else None,
            "measured_ghi": self._optional_float(row[23]) if len(row) > 23 else None,
            "clearsky": self._optional_float(row[19]),
            "direct": self._optional_float(row[17]),
            "diffuse": self._optional_float(row[18]),
            "direct_share": direct_share_of(
                self._optional_float(row[17]), self._optional_float(row[18])
            ),
        }

    def _optional_float(self, value: Any) -> Optional[float]:
        if value is None:
            return
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(number):
            return
        return number

    def _later_stamp(self, current: Optional[str], candidate: Any) -> Optional[str]:
        if candidate is None:
            return current
        candidate_stamp = str(candidate)
        if current is None or candidate_stamp > current:
            return candidate_stamp
        return current

    def _cloud_bucket(self, clouds: Optional[float]) -> str:
        effective_clouds = 50.0 if clouds is None else clouds
        if effective_clouds < 30.0:
            return "clear"
        if effective_clouds < 70.0:
            return "partly_cloudy"
        return "overcast"

    def _hour_bucket(self, hour: int) -> str:
        if hour < 10:
            return "morning"
        if hour < 15:
            return "midday"
        return "afternoon"

    async def _persist_raw_contract(
        self,
        working: Dict[str, GroupCalibrationFactors],
        blend: Dict[str, Any],
        stamp: str,
    ) -> None:
        async with self._db.transaction():
            for factor_group_name, group_factors in working.items():
                await self._execute_raw(
                    "INSERT INTO physics_calibration_groups\n                       (group_name, global_factor, sample_count, confidence,\n                        last_updated, learning_contract_version,\n                        poa_contract_stamped_at, rearm_learning_days,\n                        rearm_pending_factor, raw_damp_pending)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                       ON CONFLICT(group_name) DO UPDATE SET\n                           global_factor = excluded.global_factor,\n                           sample_count = excluded.sample_count,\n                           confidence = excluded.confidence,\n                           last_updated = excluded.last_updated,\n                           rearm_learning_days = excluded.rearm_learning_days,\n                           rearm_pending_factor = excluded.rearm_pending_factor,\n                           raw_damp_pending = excluded.raw_damp_pending",
                    (
                        factor_group_name,
                        round(group_factors.global_factor, 4),
                        group_factors.sample_count,
                        round(group_factors.confidence, 3),
                        SafeDateTimeUtil.now(),
                        group_factors.learning_contract_version,
                        None,
                        group_factors.rearm_learning_days,
                        None,
                        self._dump_raw_pending(group_factors.raw_damp_pending),
                    ),
                )
                await self._execute_raw(
                    "DELETE FROM physics_calibration_hourly WHERE group_name = ?",
                    (factor_group_name,),
                )
                for hour, hourly_factor in group_factors.hourly_factors.items():
                    await self._execute_raw(
                        "INSERT INTO physics_calibration_hourly (group_name, hour, factor)\n                           VALUES (?, ?, ?)",
                        (factor_group_name, int(hour), round(hourly_factor, 4)),
                    )
                await self._execute_raw(
                    "DELETE FROM physics_calibration_buckets WHERE group_name = ?",
                    (factor_group_name,),
                )
                await self._execute_raw(
                    "DELETE FROM physics_calibration_bucket_hourly WHERE group_name = ?",
                    (factor_group_name,),
                )
                for bucket_name, bucket_factors in group_factors.bucket_factors.items():
                    await self._execute_raw(
                        "INSERT INTO physics_calibration_buckets\n                           (group_name, bucket_name, global_factor, sample_count, confidence)\n                           VALUES (?, ?, ?, ?, ?)",
                        (
                            factor_group_name,
                            bucket_name,
                            round(bucket_factors.global_factor, 4),
                            bucket_factors.sample_count,
                            round(bucket_factors.confidence, 3),
                        ),
                    )
                    for hour, hourly_factor in bucket_factors.hourly_factors.items():
                        await self._execute_raw(
                            "INSERT INTO physics_calibration_bucket_hourly\n                               (group_name, bucket_name, hour, factor)\n                               VALUES (?, ?, ?, ?)",
                            (
                                factor_group_name,
                                bucket_name,
                                int(hour),
                                round(hourly_factor, 4),
                            ),
                        )
                continue
            await self._write_blend_tables(blend)
            await self._write_sun_shadow()
            for factor_group_name in working:
                await self._execute_raw(
                    "UPDATE physics_calibration_groups\n                       SET learning_contract_version = ?, poa_contract_stamped_at = ?\n                       WHERE group_name = ?",
                    (
                        PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT,
                        stamp,
                        factor_group_name,
                    ),
                )
        return None if True else None

    def apply_sun_shadow(
        self,
        group_name: str,
        calibrated_kwh: float,
        raw_kwh: float,
        azimuth: object,
        elevation: object,
        direct_share: object,
    ) -> float:
        """Unshaded global scale times the direct-beam shadow factor.

        An active position does not use the clock-hour factor. That factor
        is learned only on other positions, and the shadow loss belongs to
        the sun position.
        """
        shadow_book = self._sun_shadow
        if shadow_book is None or not shadow_book.is_active(
            group_name, azimuth, elevation
        ):
            return float(calibrated_kwh)
        shadow_factor = shadow_book.factor(group_name, azimuth, elevation, direct_share)
        try:
            resolved_raw_kwh = float(raw_kwh)
        except (TypeError, ValueError):
            resolved_raw_kwh = 0.0
        group_factors = self._factors.get(group_name)
        global_factor = (
            group_factors.global_factor if group_factors is not None else 1.0
        )
        if resolved_raw_kwh > 0.0 and global_factor > 0.0:
            return resolved_raw_kwh * global_factor * shadow_factor
        return float(calibrated_kwh) * shadow_factor

    async def _load_sun_shadow(self) -> None:
        if self._db is None or not hasattr(self._db, "fetchall"):
            return
        try:
            rows = await self._db.fetchall(
                "SELECT group_name, day, az_bin, elev_bin, r, direct_share, hour\n                   FROM sun_position_shadow_sample"
            )
        except Exception:
            return
        if rows:
            self._sun_shadow.load_samples(rows)
        return None if True else None

    async def _persist_sun_shadow(self) -> None:
        if self._db is None or not hasattr(self._db, "transaction"):
            return None
        if self._sun_shadow is None or not self._sun_shadow.has_samples():
            return
        try:
            async with self._db.transaction():
                await self._write_sun_shadow()
        except Exception as error:
            _LOGGER.warning(
                "Sun-position shadow persist failed; next learn retries: %s", error
            )
        return None if True else None

    async def _write_sun_shadow(self) -> None:
        await self._execute_raw(
            "CREATE TABLE IF NOT EXISTS sun_position_shadow_sample (\n                   group_name TEXT NOT NULL,\n                   day TEXT NOT NULL,\n                   az_bin INTEGER NOT NULL,\n                   elev_bin INTEGER NOT NULL,\n                   r REAL NOT NULL,\n                   direct_share REAL NOT NULL,\n                   hour INTEGER NOT NULL,\n                   PRIMARY KEY (group_name, day, az_bin, elev_bin, hour)\n               )",
            (),
        )
        await self._execute_raw(
            "CREATE TABLE IF NOT EXISTS sun_position_shadow (\n                   group_name TEXT NOT NULL,\n                   az_bin INTEGER NOT NULL,\n                   elev_bin INTEGER NOT NULL,\n                   evaluable_days INTEGER NOT NULL,\n                   median_r REAL NOT NULL,\n                   direct_share REAL NOT NULL,\n                   sample_count INTEGER NOT NULL,\n                   hour_hint INTEGER,\n                   PRIMARY KEY (group_name, az_bin, elev_bin)\n               )",
            (),
        )
        await self._execute_raw("DELETE FROM sun_position_shadow_sample", ())
        await self._execute_raw("DELETE FROM sun_position_shadow", ())
        for shadow_sample in self._sun_shadow.export_samples():
            await self._execute_raw(
                "INSERT INTO sun_position_shadow_sample\n                   (group_name, day, az_bin, elev_bin, r, direct_share, hour)\n                   VALUES (?, ?, ?, ?, ?, ?, ?)",
                shadow_sample,
            )
        for shadow_pattern in self._sun_shadow.active_patterns():
            await self._execute_raw(
                "INSERT INTO sun_position_shadow\n                   (group_name, az_bin, elev_bin, evaluable_days, median_r,\n                    direct_share, sample_count, hour_hint)\n                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    shadow_pattern.group_name,
                    shadow_pattern.az_bin,
                    shadow_pattern.elev_bin,
                    shadow_pattern.evaluable_days,
                    round(shadow_pattern.median_r, 6),
                    round(shadow_pattern.direct_share, 6),
                    shadow_pattern.sample_count,
                    shadow_pattern.hour_hint,
                ),
            )

    async def _execute_raw(self, sql: str, params: tuple) -> None:
        await self._db.execute(sql, params, auto_commit=False)

    async def _write_blend_tables(self, blend: Dict[str, Any]) -> None:
        """Update physics-owned columns. Delivered AI, TFS, LSTM, ridge and blend stay."""
        now = SafeDateTimeUtil.now()
        await self._update_method_blend(blend["method"], now)
        await self._update_group_blend(blend["group"], now)
        if blend["regime"] is None:
            return
        await self._update_regime_blend(blend["regime"], now)

    async def _update_method_blend(self, rows: list, now) -> None:
        existing_scores = await self._blend_index(
            "SELECT cloud_bucket, hour_bucket, season, ai_mae\n               FROM method_performance_learning",
            lambda row: (row[0], row[1], row[2]),
        )
        for score_row in rows:
            cloud_bucket, hour_bucket, season = score_row["key"]
            score_key = (cloud_bucket, hour_bucket, season)
            physics_mae = round(float(score_row["physics_mae"]), 6)
            if score_key in existing_scores:
                ai_mae = existing_scores[score_key][3]
                await self._execute_raw(
                    "UPDATE method_performance_learning\n                       SET physics_mae = ?, ai_advantage_factor = ?\n                       WHERE cloud_bucket = ? AND hour_bucket = ? AND season IS ?",
                    (
                        physics_mae,
                        round(advantage_factor(physics_mae, ai_mae), 4),
                        cloud_bucket,
                        hour_bucket,
                        season,
                    ),
                )
                continue
            await self._execute_raw(
                "INSERT INTO method_performance_learning\n                   (cloud_bucket, hour_bucket, season, physics_mae, ai_mae, blend_mae,\n                    ai_advantage_factor, sample_count, last_updated)\n                   VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
                (
                    cloud_bucket,
                    hour_bucket,
                    season,
                    physics_mae,
                    round(advantage_factor(physics_mae, None), 4),
                    int(score_row["sample_count"]),
                    now,
                ),
            )

    async def _update_group_blend(self, rows: list, now) -> None:
        existing_scores = await self._blend_index(
            "SELECT group_name, cloud_bucket, hour_bucket, season,\n                      ai_mae, lstm_mae, ridge_mae\n               FROM group_method_performance",
            lambda row: (row[0], row[1], row[2], row[3]),
        )
        for score_row in rows:
            group_name, cloud_bucket, hour_bucket, season = score_row["key"]
            score_key = (group_name, cloud_bucket, hour_bucket, season)
            physics_mae = round(float(score_row["physics_mae"]), 6)
            if score_key in existing_scores:
                existing_row = existing_scores[score_key]
                best_method = group_best_method(
                    physics_mae, existing_row[4], existing_row[5], existing_row[6]
                )
                await self._execute_raw(
                    "UPDATE group_method_performance\n                       SET physics_mae = ?, ai_advantage_factor = ?, best_method = ?\n                       WHERE group_name = ? AND cloud_bucket = ? AND hour_bucket = ?\n                         AND season IS ?",
                    (
                        physics_mae,
                        round(advantage_factor(physics_mae, existing_row[4]), 4),
                        best_method,
                        group_name,
                        cloud_bucket,
                        hour_bucket,
                        season,
                    ),
                )
                continue
            await self._execute_raw(
                "INSERT INTO group_method_performance\n                   (group_name, cloud_bucket, hour_bucket, season,\n                    physics_mae, ai_mae, lstm_mae, ridge_mae, blend_mae,\n                    best_method, ai_advantage_factor, sample_count, last_updated)\n                   VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?)",
                (
                    group_name,
                    cloud_bucket,
                    hour_bucket,
                    season,
                    physics_mae,
                    group_best_method(physics_mae, None, None, None),
                    round(advantage_factor(physics_mae, None), 4),
                    int(score_row["sample_count"]),
                    now,
                ),
            )

    async def _update_regime_blend(self, rows: list, now) -> None:
        existing_scores = await self._blend_index(
            "SELECT weather_regime, hour_bucket, season,\n                      ai_mae, tfs_mae, blend_mae, tfs_advantage_factor\n               FROM weather_regime_performance_learning",
            lambda row: (row[0], row[1], row[2]),
        )
        for score_row in rows:
            weather_regime, hour_bucket, season = score_row["key"]
            score_key = (weather_regime, hour_bucket, season)
            physics_mae = round(float(score_row["physics_mae"]), 6)
            physics_bias = score_row.get("physics_bias")
            if physics_bias is not None:
                physics_bias = round(float(physics_bias), 6)
            if score_key in existing_scores:
                existing_row = existing_scores[score_key]
                best_method = regime_best_method(
                    physics_mae, existing_row[5], existing_row[3], existing_row[4]
                )
                tfs_weight = recommended_tfs_weight(best_method, existing_row[6])
                await self._execute_raw(
                    "UPDATE weather_regime_performance_learning\n                       SET physics_mae = ?, physics_bias = ?,\n                           ai_advantage_factor = ?, best_method = ?,\n                           recommended_tfs_weight = ?\n                       WHERE weather_regime = ? AND hour_bucket = ? AND season IS ?",
                    (
                        physics_mae,
                        physics_bias,
                        round(advantage_factor(physics_mae, existing_row[3]), 4),
                        best_method,
                        None if tfs_weight is None else round(tfs_weight, 4),
                        weather_regime,
                        hour_bucket,
                        season,
                    ),
                )
                continue
            best_method = regime_best_method(physics_mae, None, None, None)
            await self._execute_raw(
                "INSERT INTO weather_regime_performance_learning\n                   (weather_regime, hour_bucket, season,\n                    physics_mae, ai_mae, tfs_mae, blend_mae,\n                    physics_bias, ai_bias, tfs_bias, blend_bias,\n                    best_method, recommended_tfs_weight,\n                    ai_advantage_factor, tfs_advantage_factor,\n                    sample_count, last_updated)\n                   VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL, NULL,\n                           ?, NULL, ?, 1.0, ?, ?)",
                (
                    weather_regime,
                    hour_bucket,
                    season,
                    physics_mae,
                    physics_bias,
                    best_method,
                    round(advantage_factor(physics_mae, None), 4),
                    int(score_row["sample_count"]),
                    now,
                ),
            )
            continue

    async def _blend_index(self, sql: str, key_of) -> Dict[tuple, Any]:
        rows = await self._db.fetchall(sql)
        return {key_of(row): row for row in rows or ()}
