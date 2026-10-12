# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Sun-position shadow model. Each group is compared with itself. @zara"""

from __future__ import annotations
import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

AZIMUTH_BIN_DEG = 15
ELEVATION_BIN_DEG = 10
MIN_ELEVATION_DEG = 5.0
SENSOR_KT_MIN = 0.6
SENSORLESS_DIRECT_SHARE_MIN = 0.7
SENSORLESS_CLOUD_MAX = 30.0
MIN_EVALUABLE_DAYS = 4
MEDIAN_R_MAX = 0.85
SAMPLE_R_MAX = 0.9
SAMPLE_FRACTION_MIN = 0.75
WINDOW_DAYS = 45
FACTOR_MIN = 0.05
FACTOR_MAX = 1.0
CLEAR_SKY_MIN_WM2 = 20.0
Key = Tuple[str, int, int]


@dataclass
class _Sample:
    day: str
    r: float
    direct_share: float
    hour: int


@dataclass(frozen=True)
class SunShadowPattern:
    group_name: str
    az_bin: int
    elev_bin: int
    evaluable_days: int
    median_r: float
    direct_share: float
    sample_count: int
    hour_hint: int
    active: bool


def azimuth_bin(azimuth: float) -> int:
    """Western-wrap bin start in degrees."""
    return int(math.floor(float(azimuth) % 360.0 / AZIMUTH_BIN_DEG)) * AZIMUTH_BIN_DEG


def elevation_bin(elevation: float) -> int:
    return int(math.floor(float(elevation) / ELEVATION_BIN_DEG)) * ELEVATION_BIN_DEG


def _median(values: Sequence[float]) -> float:
    ordered_values = sorted(values)
    count = len(ordered_values)
    middle = count // 2
    if count % 2:
        return float(ordered_values[middle])
    return (float(ordered_values[middle - 1]) + float(ordered_values[middle])) / 2.0


def _finite(value: object) -> Optional[float]:
    if value is None:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    if not math.isfinite(number):
        return
    return number


def _flagged(
    snow: bool, frost: object, exclude: bool, mppt: bool, exclude_group: bool
) -> bool:
    if snow or exclude or mppt or exclude_group:
        return True
    if frost in (None, False, 0, "0", "", "none", "false", "False"):
        return False
    return True


def direct_share_of(direct: object, diffuse: object) -> Optional[float]:
    direct_value = _finite(direct)
    diffuse_value = _finite(diffuse)
    if direct_value is None or diffuse_value is None:
        return None
    total_radiation = direct_value + diffuse_value
    if total_radiation <= 0.0:
        return
    return max(0.0, min(1.0, direct_value / total_radiation))


def hour_is_evaluable(
    *,
    elevation: object,
    measured_ghi: object,
    clearsky_ghi: object,
    direct: object,
    diffuse: object,
    clouds: object,
    snow: bool = False,
    frost: object = None,
    exclude: bool = False,
    mppt: bool = False,
    exclude_group: bool = False,
) -> bool:
    """Diffuse, low-sun and detector-flagged hours are not evaluable."""
    if _flagged(snow, frost, exclude, mppt, exclude_group):
        return False
    elevation_value = _finite(elevation)
    if elevation_value is None or elevation_value < MIN_ELEVATION_DEG:
        return False
    measured_ghi_value = _finite(measured_ghi)
    clearsky_ghi_value = _finite(clearsky_ghi)
    if (
        measured_ghi_value is not None
        and clearsky_ghi_value is not None
        and (clearsky_ghi_value >= CLEAR_SKY_MIN_WM2)
    ):
        return measured_ghi_value / clearsky_ghi_value >= SENSOR_KT_MIN
    direct_share = direct_share_of(direct, diffuse)
    clouds_value = _finite(clouds)
    if direct_share is None or clouds_value is None:
        return False
    return (
        direct_share >= SENSORLESS_DIRECT_SHARE_MIN
        and clouds_value <= SENSORLESS_CLOUD_MAX
    )


class SunShadowBook:
    """Samples and the active sun-position patterns derived from them."""

    def __init__(self) -> None:
        self._samples = {}

    def has_samples(self) -> bool:
        return any(self._samples.values())

    def observe(
        self,
        group: str,
        day: str,
        azimuth: float,
        elevation: float,
        ratio: float,
        direct_share: float,
        hour: int,
    ) -> None:
        if not math.isfinite(ratio) or ratio < 0.0:
            return
        if not math.isfinite(direct_share):
            return
        key = (str(group), azimuth_bin(azimuth), elevation_bin(elevation))
        self._samples.setdefault(key, []).append(
            _Sample(
                day=str(day)[:10],
                r=float(ratio),
                direct_share=max(0.0, min(1.0, float(direct_share))),
                hour=int(hour),
            )
        )

    def learning_disposition(
        self,
        *,
        group: str,
        day: str,
        hour: int,
        azimuth: object,
        elevation: object,
        actual: object,
        raw: object,
        global_factor: object,
        measured_ghi: object = None,
        clearsky_ghi: object = None,
        direct: object = None,
        diffuse: object = None,
        clouds: object = None,
        snow: bool = False,
        frost: object = None,
        exclude: bool = False,
        mppt: bool = False,
        exclude_group: bool = False,
    ) -> str:
        # fmt: off
        """Return skip, hourly, or all. Evaluable hours are recorded first.

        skip: this sun position is already an active shadow, so the hour
        must not move a calibration factor.
        hourly: known position, not evaluable, not an active shadow. The
        hour may train the clock-hour and weather factors, not the global.
        all: train every factor. Missing sun position stays here so a gap
        in the astronomy cache does not drop the group's scale.
        """
        # fmt: on
        azimuth_value = _finite(azimuth)
        elevation_value = _finite(elevation)
        active = False
        if azimuth_value is not None and elevation_value is not None:
            active = self.is_active(
                group, azimuth_value, elevation_value, before_day=str(day)[:10]
            )
        flagged = _flagged(
            bool(snow), frost, bool(exclude), bool(mppt), bool(exclude_group)
        )
        evaluable = hour_is_evaluable(
            elevation=elevation_value,
            measured_ghi=measured_ghi,
            clearsky_ghi=clearsky_ghi,
            direct=direct,
            diffuse=diffuse,
            clouds=clouds,
            snow=bool(snow),
            frost=frost,
            exclude=bool(exclude),
            mppt=bool(mppt),
            exclude_group=bool(exclude_group),
        )
        if (
            evaluable
            and azimuth_value is not None
            and (elevation_value is not None)
            and (not flagged)
        ):
            actual_value = _finite(actual)
            raw_value = _finite(raw)
            global_factor_value = _finite(global_factor)
            direct_share = direct_share_of(direct, diffuse)
            if direct_share is None:
                measured_ghi_value = _finite(measured_ghi)
                clearsky_ghi_value = _finite(clearsky_ghi)
                if (
                    measured_ghi_value is not None
                    and clearsky_ghi_value is not None
                    and (clearsky_ghi_value >= CLEAR_SKY_MIN_WM2)
                ):
                    direct_share = max(
                        0.0, min(1.0, measured_ghi_value / clearsky_ghi_value)
                    )
            if (
                actual_value is not None
                and raw_value is not None
                and (global_factor_value is not None)
                and (raw_value > 0.0)
                and (global_factor_value > 0.0)
                and (direct_share is not None)
            ):
                self.observe(
                    group,
                    str(day)[:10],
                    azimuth_value,
                    elevation_value,
                    actual_value / (raw_value * global_factor_value),
                    direct_share,
                    int(hour),
                )
        if active:
            return "skip"
        if azimuth_value is None or elevation_value is None or flagged:
            return "all"
        if not evaluable:
            return "hourly"
        return "all"

    def is_active(
        self,
        group: str,
        azimuth: object,
        elevation: object,
        before_day: Optional[str] = None,
    ) -> bool:
        return (
            self.pattern(group, azimuth, elevation, before_day=before_day) is not None
        )

    def pattern(
        self,
        group: str,
        azimuth: object,
        elevation: object,
        before_day: Optional[str] = None,
    ) -> Optional[SunShadowPattern]:
        azimuth_value = _finite(azimuth)
        elevation_value = _finite(elevation)
        if azimuth_value is None or elevation_value is None:
            return None
        azimuth_key = azimuth_bin(azimuth_value)
        elevation_key = elevation_bin(elevation_value)
        pattern = self._pattern_at(str(group), azimuth_key, elevation_key, before_day)
        if pattern is not None:
            return pattern if pattern.active else None
        return self._neighbor_pattern(
            str(group), azimuth_key, elevation_key, before_day
        )

    def factor(
        self,
        group: str,
        azimuth: object,
        elevation: object,
        forecast_direct_share: object,
        before_day: Optional[str] = None,
    ) -> float:
        """Loss on the direct beam only. Overcast forecasts stay near 1."""
        result = self.pattern(group, azimuth, elevation, before_day=before_day)
        if result is None or not result.active:
            return FACTOR_MAX
        reduction = 1.0 - result.median_r
        if reduction <= 0.0 or result.direct_share <= 0.0:
            return FACTOR_MAX
        direct_share = _finite(forecast_direct_share)
        if direct_share is None or direct_share <= 0.0:
            return FACTOR_MAX
        adjusted_factor = 1.0 - reduction * (
            min(1.0, direct_share) / result.direct_share
        )
        return min(FACTOR_MAX, max(FACTOR_MIN, adjusted_factor))

    def active_count(self, group: str) -> int:
        return sum(
            (1 for pattern in self.active_patterns() if pattern.group_name == group)
        )

    def active_patterns(
        self, before_day: Optional[str] = None
    ) -> List[SunShadowPattern]:
        result = []
        for group_name, azimuth_key, elevation_key in list(self._samples):
            pattern = self._pattern_at(
                group_name, azimuth_key, elevation_key, before_day
            )
            if pattern is not None and pattern.active:
                result.append(pattern)
        result.sort(key=lambda item: (item.group_name, item.elev_bin, item.az_bin))
        return result

    def export_samples(self) -> List[Tuple[str, str, int, int, float, float, int]]:
        rows = []
        anchor = self._anchor(None)
        for (group_name, azimuth_key, elevation_key), samples in self._samples.items():
            for sample in samples:
                if self._in_window(sample.day, None, anchor):
                    rows.append(
                        (
                            group_name,
                            sample.day,
                            azimuth_key,
                            elevation_key,
                            round(sample.r, 6),
                            round(sample.direct_share, 6),
                            sample.hour,
                        )
                    )
        return rows

    def load_samples(self, rows: Sequence[Sequence[object]]) -> None:
        self._samples.clear()
        for row in rows:
            try:
                self.observe(
                    str(row[0]),
                    str(row[1]),
                    float(row[2]) + AZIMUTH_BIN_DEG / 2.0,
                    float(row[3]) + ELEVATION_BIN_DEG / 2.0,
                    float(row[4]),
                    float(row[5]),
                    int(row[6]),
                )
            except (TypeError, ValueError, IndexError):
                continue

    def _neighbor_pattern(
        self, group: str, az_key: int, el_key: int, before_day: Optional[str]
    ) -> Optional[SunShadowPattern]:
        neighbors = []
        for offset in (-AZIMUTH_BIN_DEG, AZIMUTH_BIN_DEG):
            pattern = self._pattern_at(
                group, (az_key + offset) % 360, el_key, before_day
            )
            if pattern is not None and pattern.active:
                neighbors.append(pattern)
        if not neighbors:
            return
        if len(neighbors) == 1:
            return neighbors[0]
        median_ratio = sum((neighbor.median_r for neighbor in neighbors)) / len(
            neighbors
        )
        average_direct_share = sum(
            (neighbor.direct_share for neighbor in neighbors)
        ) / len(neighbors)
        days = max((neighbor.evaluable_days for neighbor in neighbors))
        count = sum((neighbor.sample_count for neighbor in neighbors))
        hour_hint = _median([float(neighbor.hour_hint) for neighbor in neighbors])
        combined_pattern = SunShadowPattern(
            group_name=group,
            az_bin=az_key,
            elev_bin=el_key,
            evaluable_days=days,
            median_r=median_ratio,
            direct_share=average_direct_share,
            sample_count=count,
            hour_hint=int(round(hour_hint)),
            active=True,
        )
        return combined_pattern if combined_pattern.median_r <= MEDIAN_R_MAX else None

    def _pattern_at(
        self, group: str, az_key: int, el_key: int, before_day: Optional[str]
    ) -> Optional[SunShadowPattern]:
        samples = self._samples.get((group, az_key, el_key))
        if not samples:
            return
        anchor = self._anchor(before_day)
        window_samples = [
            sample
            for sample in samples
            if self._in_window(sample.day, before_day, anchor)
        ]
        if not window_samples:
            return
        days = {sample.day for sample in window_samples}
        ratios = [sample.r for sample in window_samples]
        median_ratio = _median(ratios)
        shadow_samples = sum((1 for ratio in ratios if ratio <= SAMPLE_R_MAX))
        active = (
            len(days) >= MIN_EVALUABLE_DAYS
            and median_ratio <= MEDIAN_R_MAX
            and (shadow_samples / len(ratios) >= SAMPLE_FRACTION_MIN)
        )
        return SunShadowPattern(
            group_name=group,
            az_bin=az_key,
            elev_bin=el_key,
            evaluable_days=len(days),
            median_r=median_ratio,
            direct_share=_median([sample.direct_share for sample in window_samples]),
            sample_count=len(window_samples),
            hour_hint=int(
                round(_median([float(sample.hour) for sample in window_samples]))
            ),
            active=active,
        )

    def _anchor(self, before_day: Optional[str]) -> Optional[str]:
        if before_day:
            return str(before_day)[:10]
        latest_day = None
        for samples in self._samples.values():
            for sample in samples:
                if latest_day is None or sample.day > latest_day:
                    latest_day = sample.day
        return latest_day

    def _in_window(
        self, day: str, before_day: Optional[str], anchor: Optional[str]
    ) -> bool:
        if before_day is not None and day >= str(before_day)[:10]:
            return False
        if anchor is None:
            return True
        try:
            sample_day = date.fromisoformat(day[:10])
            anchor_day = date.fromisoformat(anchor[:10])
        except ValueError:
            return False
        if before_day is not None:
            anchor_day = anchor_day - timedelta(days=1)
        return sample_day >= anchor_day - timedelta(days=WINDOW_DAYS - 1)
