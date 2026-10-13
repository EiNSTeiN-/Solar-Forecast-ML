# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class PhysicsCalibrator:

    async def _load_history(self) -> None:
        """Load calibration history from database. @zara"""
        try:
            _var_var_19 = (
                (SafeDateTimeUtil.now() - timedelta(days=ROLLING_WINDOW_DAYS))
                .date()
                .isoformat()
            )
            _var_var_2 = await self._db.fetchall(
                "SELECT date, group_name, bucket_name, hour, avg_ratio, sample_count\n                   FROM physics_calibration_history\n                   WHERE date >= ?\n                   ORDER BY date, group_name",
                (_var_var_19,),
            )
            _var_var_32 = {}
            for _var_var_3 in _var_var_2:
                _var_var_33 = str(_var_var_3[0])
                _var_var_4 = _var_var_3[1]
                _var_var_11 = _var_var_3[2]
                _var_var_34 = _var_var_3[3]
                _var_var_35 = _var_var_3[4]
                _var_var_36 = _var_var_3[5]
                if _var_var_33 not in _var_var_32:
                    _var_var_32[_var_var_33] = {"date": _var_var_33, "groups": {}}
                if _var_var_4 not in _var_var_32[_var_var_33]["groups"]:
                    _var_var_32[_var_var_33]["groups"][_var_var_4] = {
                        "hourly": {},
                        "sample_count": 0,
                        "buckets": {},
                    }
                if _var_var_11:
                    if (
                        _var_var_11
                        not in _var_var_32[_var_var_33]["groups"][_var_var_4]["buckets"]
                    ):
                        _var_var_32[_var_var_33]["groups"][_var_var_4]["buckets"][
                            _var_var_11
                        ] = {"hourly": {}, "sample_count": 0}
                    if _var_var_34 is not None:
                        _var_var_32[_var_var_33]["groups"][_var_var_4]["buckets"][
                            _var_var_11
                        ]["hourly"][str(_var_var_34)] = _var_var_35
                        _var_var_32[_var_var_33]["groups"][_var_var_4]["buckets"][
                            _var_var_11
                        ]["sample_count"] += _var_var_36
                        continue
                    continue
                if _var_var_34 is not None:
                    _var_var_32[_var_var_33]["groups"][_var_var_4]["hourly"][
                        str(_var_var_34)
                    ] = _var_var_35
                    _var_var_32[_var_var_33]["groups"][_var_var_4][
                        "sample_count"
                    ] += _var_var_36
                    continue
            self._history = list(_var_var_32.values())
        except Exception as _var_var_1:
            _LOGGER.warning(f"Could not load calibration history: {_var_var_1}")
            self._history = []
        return None if True else None

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
            _var_var_63 = 0
            _var_var_64 = {}
            _var_var_65 = {}
            _var_var_66 = {}
            _var_var_67 = {}
            _var_var_68 = []
            _var_var_69 = []
            for _var_var_4, _var_var_70 in group_data.items():
                _var_var_64[_var_var_4] = []
                _var_var_65[_var_var_4] = {}
                _var_var_66[_var_var_4] = {
                    _var_var_71.value: [] for _var_var_71 in WeatherBucket
                }
                _var_var_67[_var_var_4] = {
                    _var_var_71.value: {} for _var_var_71 in WeatherBucket
                }
                _var_var_17 = self._factors.get(_var_var_4)
                if _var_var_17 is None:
                    _var_var_17 = GroupCalibrationFactors(
                        group_name=_var_var_4,
                        learning_contract_version=PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT,
                        rearm_learning_days=0,
                        poa_contract_stamped_at=(
                            SafeDateTimeUtil.now if True else SafeDateTimeUtil.now
                        )().isoformat(),
                    )
                    self._factors[_var_var_4] = _var_var_17
                elif _var_var_17.learning_contract_version in (
                    PHYSICS_CALIBRATION_POA_DNI_IAM_CONTRACT,
                    PHYSICS_CALIBRATION_GHI_FORECAST_SCALE_CONTRACT,
                    PHYSICS_CALIBRATION_GHI_TILT_POA_CONTRACT,
                    PHYSICS_CALIBRATION_RAW_FORECAST_V1_CONTRACT,
                    PHYSICS_CALIBRATION_RAW_FORECAST_V2_CONTRACT,
                    PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT,
                ) and (not _var_var_17.poa_contract_stamped_at):
                    _var_var_17.poa_contract_stamped_at = (
                        SafeDateTimeUtil.now if True else SafeDateTimeUtil.now
                    )().isoformat()
                for _var_var_72 in _var_var_70:
                    _var_var_72.setdefault("date", date)
                    if not poa_contract_hour_is_learnable(
                        _var_var_17.learning_contract_version,
                        _var_var_17.poa_contract_stamped_at,
                        _var_var_72.get("prediction_created_at"),
                    ):
                        continue
                    _var_var_34 = _var_var_72.get("hour", 0)
                    _var_var_73 = _var_var_72.get("physics_kwh", 0)
                    _var_var_74 = _var_var_72.get("actual_kwh", 0)
                    _var_var_75 = _var_var_72.get(
                        "actual_cloud_cover"
                    ) or _var_var_72.get("cloud_cover")
                    _var_var_76 = _var_var_72.get("sun_elevation")
                    _var_var_77 = _var_var_72.get("visibility_m")
                    _var_var_78 = _var_var_72.get("precipitation_mm")
                    _var_var_79 = _var_var_72.get("temperature_c")
                    _var_var_80 = _var_var_72.get("snow_covered_panels", False)
                    _var_var_81 = _var_var_72.get("frost_detected")
                    _var_var_82 = _var_var_72.get("frost_score")
                    _var_var_83 = 7 <= _var_var_34 <= 10
                    _var_var_84 = (
                        MIN_PRODUCTION_KWH_MORNING
                        if _var_var_83
                        else MIN_PRODUCTION_KWH
                    )
                    _var_var_85 = _var_var_73
                    _var_var_86 = PHYSICS_REF_KIND_FORECAST
                    if is_raw_forecast_contract(_var_var_17.learning_contract_version):
                        _var_var_87 = _var_var_72.get("raw_physics_kwh")
                        if _var_var_87 is None:
                            continue
                        try:
                            _var_var_85 = float(_var_var_87)
                        except (TypeError, ValueError):
                            continue
                        if not (math.isfinite if True else math.isfinite)(_var_var_85):
                            continue
                    if _var_var_85 < _var_var_84 or _var_var_74 < _var_var_84:
                        continue
                    _var_var_88 = _var_var_74 / _var_var_85
                    _var_var_89 = _var_var_88
                    _var_var_90 = 0
                    if (
                        _var_var_89 < MIN_CORRECTION_FACTOR
                        or _var_var_89 > MAX_CORRECTION_FACTOR
                    ):
                        _LOGGER.debug(
                            f"Clamping outlier: {_var_var_4} hour {_var_var_34}, ratio={_var_var_89:.2f} -> {max(MIN_CORRECTION_FACTOR, min(MAX_CORRECTION_FACTOR, _var_var_89)):.2f}"
                        )
                        _var_var_89 = max(
                            MIN_CORRECTION_FACTOR,
                            min(MAX_CORRECTION_FACTOR, _var_var_89),
                        )
                        _var_var_90 = 1
                        _var_var_69.append(
                            {
                                "date": date,
                                "group_name": _var_var_4,
                                "hour": _var_var_34,
                                "bucket_name": None,
                                "clamp_kind": "ratio",
                                "unclamped_value": _var_var_88,
                                "clamped_value": _var_var_89,
                            }
                        )
                    _var_var_91 = "all"
                    if (
                        is_raw_forecast_contract(_var_var_17.learning_contract_version)
                        and self._sun_shadow is not None
                    ):
                        _var_var_91 = self._sun_shadow.learning_disposition(
                            group=_var_var_4,
                            day=str(date),
                            hour=int(_var_var_34),
                            azimuth=_var_var_72.get("sun_azimuth"),
                            elevation=_var_var_76,
                            actual=_var_var_74,
                            raw=_var_var_85,
                            global_factor=_var_var_17.global_factor,
                            measured_ghi=_var_var_72.get("actual_ghi"),
                            clearsky_ghi=_var_var_72.get("clear_sky_ghi"),
                            direct=_var_var_72.get("direct_radiation"),
                            diffuse=_var_var_72.get("diffuse_radiation"),
                            clouds=_var_var_75,
                            snow=bool(_var_var_80),
                            frost=_var_var_81,
                            exclude=bool(_var_var_72.get("exclude_from_learning")),
                            mppt=bool(_var_var_72.get("mppt_throttled")),
                            exclude_group=bool(
                                _var_var_72.get("exclude_from_learning_group")
                            ),
                        )
                    if _var_var_91 == "skip":
                        continue
                    if _var_var_91 != "hourly":
                        _var_var_64[_var_var_4].append(_var_var_89)
                    if _var_var_34 not in _var_var_65[_var_var_4]:
                        _var_var_65[_var_var_4][_var_var_34] = []
                    _var_var_65[_var_var_4][_var_var_34].append(_var_var_89)
                    _var_var_11 = None
                    if (
                        _var_var_75 is not None
                        or _var_var_80
                        or _var_var_78
                        or (_var_var_77 is not None)
                        or (_var_var_81 and _var_var_81 not in ("none", False))
                        or _var_var_82
                    ):
                        _var_var_53 = self._classify_weather(
                            _var_var_75 if _var_var_75 is not None else 50.0,
                            _var_var_76,
                            _var_var_34,
                            _var_var_78,
                            _var_var_80,
                            _var_var_77,
                            _var_var_79,
                            _var_var_81,
                            _var_var_82,
                        )
                        _var_var_11 = _var_var_53.value
                        _var_var_66[_var_var_4][_var_var_53.value].append(_var_var_89)
                        if (
                            _var_var_34
                            not in _var_var_67[_var_var_4][_var_var_53.value]
                        ):
                            _var_var_67[_var_var_4][_var_var_53.value][_var_var_34] = []
                        _var_var_67[_var_var_4][_var_var_53.value][_var_var_34].append(
                            _var_var_89
                        )
                    _var_var_68.append(
                        {
                            "date": date,
                            "group_name": _var_var_4,
                            "hour": _var_var_34,
                            "physics_ref_kind": _var_var_86,
                            "physics_ref_kwh": _var_var_85,
                            "actual_kwh": _var_var_74,
                            "unclamped_ratio": _var_var_88,
                            "clamped_ratio": _var_var_89,
                            "ratio_clamp_hit": _var_var_90,
                            "bucket_name": _var_var_11,
                        }
                    )
                    _var_var_63 += 1
                    continue
            _var_var_92 = {}
            for _var_var_4 in _var_var_64:
                if not _var_var_64[_var_var_4] and (not _var_var_65[_var_var_4]):
                    continue
                if _var_var_4 not in self._factors:
                    self._factors[_var_var_4] = GroupCalibrationFactors(
                        group_name=_var_var_4,
                        learning_contract_version=PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT,
                        rearm_learning_days=0,
                        poa_contract_stamped_at=(
                            SafeDateTimeUtil.now if True else SafeDateTimeUtil.now
                        )().isoformat(),
                    )
                _var_var_17 = self._factors[_var_var_4]
                if is_raw_forecast_contract(_var_var_17.learning_contract_version):
                    _var_var_92[_var_var_4] = self._update_raw_damp(
                        _var_var_17,
                        _var_var_64[_var_var_4],
                        _var_var_65[_var_var_4],
                        _var_var_66[_var_var_4],
                        _var_var_67[_var_var_4],
                    )
                    continue
                _var_var_92[_var_var_4] = {
                    "hourly": {},
                    "sample_count": 0,
                    "buckets": {},
                }
                _var_var_93 = sum(_var_var_64[_var_var_4]) / len(
                    _var_var_64[_var_var_4]
                )
                if _var_var_17.rearm_learning_days is not None:
                    if _var_var_17.rearm_learning_days < TILT_REARM_MIN_DAYS:
                        if _var_var_17.rearm_learning_days < 1:
                            _var_var_17.rearm_learning_days = 1
                            _var_var_17.rearm_pending_factor = _var_var_93
                            _var_var_17.global_factor = 1.0
                            continue
                        _var_var_18 = _var_var_17.rearm_pending_factor
                        _var_var_17.global_factor = (
                            1.0 if _var_var_18 is None else _var_var_18
                        )
                        if _var_var_17.sample_count < EMA_MIN_SAMPLES_FOR_EMA:
                            _var_var_17.sample_count = EMA_MIN_SAMPLES_FOR_EMA
                        _var_var_17.rearm_learning_days = None
                        _var_var_17.rearm_pending_factor = None
                _var_var_94 = self._adaptive_alpha(_var_var_93)
                if _var_var_17.sample_count >= EMA_MIN_SAMPLES_FOR_EMA:
                    _var_var_95 = (
                        _var_var_94 * _var_var_93
                        + (1 - _var_var_94) * _var_var_17.global_factor
                    )
                else:
                    _var_var_95 = _var_var_93
                _var_var_96 = max(
                    MIN_CORRECTION_FACTOR, min(MAX_CORRECTION_FACTOR, _var_var_95)
                )
                if _var_var_96 != _var_var_95:
                    _var_var_69.append(
                        {
                            "date": date,
                            "group_name": _var_var_4,
                            "hour": None,
                            "bucket_name": None,
                            "clamp_kind": "global_factor",
                            "unclamped_value": _var_var_95,
                            "clamped_value": _var_var_96,
                        }
                    )
                _var_var_17.global_factor = _var_var_96
                _var_var_17.sample_count += len(_var_var_64[_var_var_4])
                for _var_var_34, _var_var_97 in _var_var_65[_var_var_4].items():
                    if not _var_var_97:
                        continue
                    _var_var_98 = sum(_var_var_97) / len(_var_var_97)
                    if _var_var_34 in _var_var_17.hourly_factors:
                        _var_var_99 = (
                            _var_var_94 * _var_var_98
                            + (1 - _var_var_94)
                            * _var_var_17.hourly_factors[_var_var_34]
                        )
                    else:
                        _var_var_99 = _var_var_98
                    _var_var_100 = max(
                        MIN_CORRECTION_FACTOR, min(MAX_CORRECTION_FACTOR, _var_var_99)
                    )
                    if _var_var_100 != _var_var_99:
                        _var_var_69.append(
                            {
                                "date": date,
                                "group_name": _var_var_4,
                                "hour": _var_var_34,
                                "bucket_name": None,
                                "clamp_kind": "hourly_factor",
                                "unclamped_value": _var_var_99,
                                "clamped_value": _var_var_100,
                            }
                        )
                    _var_var_17.hourly_factors[_var_var_34] = _var_var_100
                    _var_var_92[_var_var_4]["hourly"][str(_var_var_34)] = _var_var_98
                    _var_var_92[_var_var_4]["sample_count"] += len(_var_var_97)
                    continue
                for _var_var_101, _var_var_97 in _var_var_66[_var_var_4].items():
                    if not _var_var_97:
                        continue
                    if _var_var_101 not in _var_var_17.bucket_factors:
                        _var_var_17.bucket_factors[_var_var_101] = BucketFactors()
                    _var_var_39 = _var_var_17.bucket_factors[_var_var_101]
                    _var_var_102 = sum(_var_var_97) / len(_var_var_97)
                    if _var_var_39.sample_count >= EMA_MIN_SAMPLES_FOR_EMA:
                        _var_var_103 = (
                            _var_var_94 * _var_var_102
                            + (1 - _var_var_94) * _var_var_39.global_factor
                        )
                    else:
                        _var_var_103 = _var_var_102
                    _var_var_104 = self._clamp_bucket_factor(_var_var_101, _var_var_103)
                    if _var_var_104 != _var_var_103:
                        _var_var_69.append(
                            {
                                "date": date,
                                "group_name": _var_var_4,
                                "hour": None,
                                "bucket_name": _var_var_101,
                                "clamp_kind": "bucket_global",
                                "unclamped_value": _var_var_103,
                                "clamped_value": _var_var_104,
                            }
                        )
                    _var_var_39.global_factor = _var_var_104
                    _var_var_39.sample_count += len(_var_var_97)
                    _var_var_39.confidence = min(
                        1.0, _var_var_39.sample_count / MIN_SAMPLES_FOR_FULL_CONFIDENCE
                    )
                    _var_var_92[_var_var_4]["buckets"][_var_var_101] = {
                        "hourly": {},
                        "sample_count": len(_var_var_97),
                    }
                    for _var_var_34, _var_var_105 in _var_var_67[_var_var_4][
                        _var_var_101
                    ].items():
                        if not _var_var_105:
                            continue
                        _var_var_106 = sum(_var_var_105) / len(_var_var_105)
                        if _var_var_34 in _var_var_39.hourly_factors:
                            _var_var_107 = (
                                _var_var_94 * _var_var_106
                                + (1 - _var_var_94)
                                * _var_var_39.hourly_factors[_var_var_34]
                            )
                        else:
                            _var_var_107 = _var_var_106
                        _var_var_108 = self._clamp_bucket_factor(
                            _var_var_101, _var_var_107
                        )
                        if _var_var_108 != _var_var_107:
                            _var_var_69.append(
                                {
                                    "date": date,
                                    "group_name": _var_var_4,
                                    "hour": _var_var_34,
                                    "bucket_name": _var_var_101,
                                    "clamp_kind": "bucket_hourly",
                                    "unclamped_value": _var_var_107,
                                    "clamped_value": _var_var_108,
                                }
                            )
                        _var_var_39.hourly_factors[_var_var_34] = _var_var_108
                        _var_var_92[_var_var_4]["buckets"][_var_var_101]["hourly"][
                            str(_var_var_34)
                        ] = _var_var_106
                    continue
                _var_var_17.confidence = min(
                    1.0,
                    _var_var_17.sample_count
                    / (
                        MIN_SAMPLES_FOR_FULL_CONFIDENCE
                        * len(_var_var_17.hourly_factors)
                        if _var_var_17.hourly_factors
                        else MIN_SAMPLES_FOR_FULL_CONFIDENCE
                    ),
                )
                _var_var_17.last_updated = (
                    SafeDateTimeUtil.now if True else SafeDateTimeUtil.now
                )().isoformat()
                continue
            await self._save_config()
            await self._persist_sun_shadow()
            await self._save_history(date, _var_var_92)
            await self._save_calibration_telemetry(date, _var_var_68, _var_var_69)
            _var_var_109 = 1.0
            if self._factors:
                _var_var_109 = sum(
                    (
                        _var_var_16.global_factor
                        for _var_var_16 in self._factors.values()
                    )
                ) / len(self._factors)
            return CalibrationResult(
                success=True,
                message=f"Calibrated {len(_var_var_64)} groups with {_var_var_63} samples",
                groups_calibrated=len(_var_var_64),
                total_samples=_var_var_63,
                avg_correction_factor=round(_var_var_109, 3),
            )
        except Exception as _var_var_1:
            _LOGGER.error(f"Calibration failed: {_var_var_1}", exc_info=True)
            return CalibrationResult(success=False, message=str(_var_var_1))

    async def recalculate_factors(self) -> None:
        """Recalculate all factors from history. @zara"""
        try:
            _var_var_111 = {
                _var_var_110: _var_var_17
                for _var_var_110, _var_var_17 in self._factors.items()
                if not should_rebuild_factors_from_history(
                    _var_var_17.learning_contract_version
                )
            }
            self._factors = {}
            await self._load_history()
            for _var_var_112 in self._history:
                _var_var_0 = _var_var_112.get("groups", {})
                for _var_var_4, _var_var_113 in _var_var_0.items():
                    if _var_var_4 in _var_var_111:
                        continue
                    if _var_var_4 not in self._factors:
                        self._factors[_var_var_4] = GroupCalibrationFactors(
                            group_name=_var_var_4,
                            learning_contract_version=PHYSICS_CALIBRATION_SENSOR_GHI_CONTRACT,
                        )
                    _var_var_17 = self._factors[_var_var_4]
                    for hour_key, value in _var_var_113.get("hourly", {}).items():
                        hour = int(hour_key)
                        alpha = self._adaptive_alpha(value)
                        if hour not in _var_var_17.hourly_factors:
                            _var_var_17.hourly_factors[hour] = value
                        else:
                            _var_var_17.hourly_factors[hour] = (
                                alpha * value
                                + (1 - alpha) * _var_var_17.hourly_factors[hour]
                            )
                        _var_var_17.sample_count += 1
                    for bucket_key, bucket_values in _var_var_113.get(
                        "buckets", {}
                    ).items():
                        if bucket_key not in _var_var_17.bucket_factors:
                            _var_var_17.bucket_factors[bucket_key] = BucketFactors()
                        bucket = _var_var_17.bucket_factors[bucket_key]
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
                    if _var_var_17.hourly_factors and _var_var_17.global_factor == 1.0:
                        _var_var_17.global_factor = sum(
                            _var_var_17.hourly_factors.values()
                        ) / len(_var_var_17.hourly_factors)
                    _var_var_17.confidence = min(
                        1.0,
                        _var_var_17.sample_count
                        / (
                            MIN_SAMPLES_FOR_FULL_CONFIDENCE
                            * len(_var_var_17.hourly_factors)
                            if _var_var_17.hourly_factors
                            else MIN_SAMPLES_FOR_FULL_CONFIDENCE
                        ),
                    )
                    _var_var_17.last_updated = (
                        SafeDateTimeUtil.now if True else SafeDateTimeUtil.now
                    )().isoformat()
                continue
            self._factors.update(_var_var_111)
            await self._save_config()
            _LOGGER.info("Recalculated calibration factors from history")
        except Exception as _var_var_1:
            _LOGGER.error(f"Factor recalculation failed: {_var_var_1}")
        if True:
            return

    def _replay_raw_contract(self, rows: List[Any]):
        _var_var_135 = {
            _var_var_110: GroupCalibrationFactors(
                group_name=_var_var_110,
                learning_contract_version=PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT,
                rearm_learning_days=0,
            )
            for _var_var_110 in self._factors
        }
        _var_var_142 = {_var_var_110: set() for _var_var_110 in _var_var_135}
        _var_var_143 = {}
        for _var_var_3 in rows:
            if not _var_var_3 or _var_var_3[1] not in _var_var_135:
                continue
            _var_var_143.setdefault(str(_var_var_3[0]), []).append(_var_var_3)
        _var_var_144 = []
        _var_var_145 = []
        _var_var_146 = []
        _var_var_138 = None
        _var_var_147 = self._regime_classifier()
        _var_var_148 = self._factors
        self._factors = _var_var_135
        try:
            for _var_var_149 in sorted(_var_var_143):
                _var_var_150 = int(str(_var_var_149)[5:7])
                _var_var_151 = get_season(_var_var_150)
                _var_var_152 = {}
                _var_var_153 = {}
                _var_var_154 = {}
                _var_var_155 = {}
                for _var_var_3 in _var_var_143[_var_var_149]:
                    _var_var_156 = self._raw_replay_hour(_var_var_3)
                    if _var_var_156 is None:
                        continue
                    _var_var_138 = self._later_stamp(
                        _var_var_138, _var_var_156["created_at"]
                    )
                    _var_var_110 = _var_var_156["group"]
                    hour = _var_var_156["hour"]
                    replay_factor = _var_var_135[_var_var_110]
                    disposition = "all"
                    if self._sun_shadow is not None:
                        disposition = self._sun_shadow.learning_disposition(
                            group=_var_var_110,
                            day=str(_var_var_149),
                            hour=hour,
                            azimuth=_var_var_156["azimuth"],
                            elevation=_var_var_156["elevation"],
                            actual=_var_var_156["actual"],
                            raw=_var_var_156["raw"],
                            global_factor=replay_factor.global_factor,
                            measured_ghi=_var_var_156["measured_ghi"],
                            clearsky_ghi=_var_var_156["clearsky"],
                            direct=_var_var_156["direct"],
                            diffuse=_var_var_156["diffuse"],
                            clouds=_var_var_156["clouds"],
                            snow=_var_var_156["snow"],
                            frost=_var_var_156["frost"],
                        )
                    if disposition == "skip":
                        correction = (
                            replay_factor.global_factor
                            * self._sun_shadow.factor(
                                _var_var_110,
                                _var_var_156["azimuth"],
                                _var_var_156["elevation"],
                                _var_var_156["direct_share"],
                                before_day=str(_var_var_149),
                            )
                        )
                    else:
                        correction = self.get_correction_factor(
                            _var_var_110,
                            hour,
                            cloud_cover=_var_var_156["clouds"],
                            sun_elevation=_var_var_156["elevation"],
                            visibility_m=_var_var_156["visibility"],
                            precipitation_mm=_var_var_156["rain"],
                            temperature_c=_var_var_156["temperature"],
                            snow_covered_panels=_var_var_156["snow"],
                            frost_detected=_var_var_156["frost"],
                            frost_score=_var_var_156["frost_score"],
                        )
                        learning = _var_var_152.setdefault(
                            _var_var_110,
                            {
                                "daily": [],
                                "hourly": {},
                                "buckets": {},
                                "bucket_hourly": {},
                            },
                        )
                        if disposition != "hourly":
                            learning["daily"].append(_var_var_156["ratio"])
                        learning["hourly"].setdefault(hour, []).append(
                            _var_var_156["ratio"]
                        )
                        bucket = _var_var_156["bucket"]
                        if bucket is not None:
                            learning["buckets"].setdefault(bucket, []).append(
                                _var_var_156["ratio"]
                            )
                            learning["bucket_hourly"].setdefault(bucket, {}).setdefault(
                                hour, []
                            ).append(_var_var_156["ratio"])
                    _var_var_142[_var_var_110].add(_var_var_149)
                    corrected = correction * _var_var_156["raw"]
                    cloud = self._cloud_bucket(_var_var_156["clouds"])
                    hour_bucket = self._hour_bucket(hour)
                    group_key = (_var_var_110, cloud, hour_bucket)
                    errors = _var_var_154.setdefault(
                        group_key, {"physics": [], "ai": [], "bias": []}
                    )
                    error = corrected - _var_var_156["actual"]
                    errors["physics"].append(abs(error))
                    errors["bias"].append(error)
                    if _var_var_156["group_ai"] is not None:
                        errors["ai"].append(
                            abs(_var_var_156["group_ai"] - _var_var_156["actual"])
                        )
                    plant = _var_var_153.setdefault(
                        hour,
                        {
                            "physics": 0.0,
                            "actual": _var_var_156["plant_actual"],
                            "ai": _var_var_156["plant_ai"],
                            "cloud": cloud,
                            "hour_bucket": hour_bucket,
                            "regime": None,
                        },
                    )
                    plant["physics"] += corrected
                    if _var_var_156["plant_actual"] is not None:
                        plant["actual"] = _var_var_156["plant_actual"]
                    if (
                        _var_var_147 is not None
                        and _var_var_156["regime_inputs"] is not None
                    ):
                        plant["regime"] = _var_var_147(**_var_var_156["regime_inputs"])
                for _var_var_110, learning_data in _var_var_152.items():
                    self._update_raw_damp(
                        _var_var_135[_var_var_110],
                        learning_data["daily"],
                        learning_data["hourly"],
                        learning_data["buckets"],
                        learning_data["bucket_hourly"],
                    )
                for hour, plant_data in _var_var_153.items():
                    if plant_data["actual"] is None:
                        continue
                    score_key = (plant_data["cloud"], plant_data["hour_bucket"])
                    error = plant_data["physics"] - plant_data["actual"]
                    physics_errors = [abs(error)]
                    biases = [error]
                    ai_errors = []
                    if plant_data["ai"] is not None:
                        ai_errors = [abs(plant_data["ai"] - plant_data["actual"])]
                    _var_var_144.append(
                        (score_key + (None,), physics_errors, ai_errors, biases)
                    )
                    _var_var_144.append(
                        (score_key + (_var_var_151,), physics_errors, ai_errors, biases)
                    )
                    if plant_data["regime"]:
                        regime_key = (plant_data["regime"], plant_data["hour_bucket"])
                        _var_var_155.setdefault(
                            regime_key, {"physics": [], "ai": [], "bias": []}
                        )
                        _var_var_155[regime_key]["physics"].extend(physics_errors)
                        _var_var_155[regime_key]["ai"].extend(ai_errors)
                        _var_var_155[regime_key]["bias"].extend(biases)
                for score_key, scores in _var_var_154.items():
                    _var_var_145.append(
                        (
                            score_key + (None,),
                            scores["physics"],
                            scores["ai"],
                            scores["bias"],
                        )
                    )
                    _var_var_145.append(
                        (
                            score_key + (_var_var_151,),
                            scores["physics"],
                            scores["ai"],
                            scores["bias"],
                        )
                    )
                for score_key, scores in _var_var_155.items():
                    _var_var_146.append(
                        (
                            score_key + (None,),
                            scores["physics"],
                            scores["ai"],
                            scores["bias"],
                        )
                    )
                    _var_var_146.append(
                        (
                            score_key + (_var_var_151,),
                            scores["physics"],
                            scores["ai"],
                            scores["bias"],
                        )
                    )
        finally:
            self._factors = _var_var_148
        if _var_var_138 is None:
            _var_var_138 = (
                SafeDateTimeUtil.now if True else SafeDateTimeUtil.now
            )().isoformat()
        _var_var_136 = {}
        for _var_var_110, _var_var_17 in _var_var_135.items():
            if len(_var_var_142[_var_var_110]) < RAW_LEARN_DAYS:
                _var_var_17.global_factor = 1.0
                _var_var_17.hourly_factors = {}
                _var_var_17.bucket_factors = {}
                _var_var_17.raw_damp_pending = {}
                _var_var_17.rearm_learning_days = 0
                _var_var_17.sample_count = 0
                _var_var_17.confidence = 0.0
                _var_var_136[_var_var_110] = "B"
            else:
                _var_var_136[_var_var_110] = "A"
            _var_var_17.learning_contract_version = (
                PHYSICS_CALIBRATION_RAW_FORECAST_CONTRACT
            )
            _var_var_17.poa_contract_stamped_at = _var_var_138
        _var_var_137 = {
            "method": self._rescore_blend(_var_var_144),
            "group": self._rescore_blend(_var_var_145),
            "regime": (
                self._rescore_blend(_var_var_146) if _var_var_147 is not None else None
            ),
        }
        return (_var_var_135, _var_var_136, _var_var_137, _var_var_138)
