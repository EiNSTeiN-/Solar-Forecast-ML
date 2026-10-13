# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Drift Monitor for Solar Forecast ML V17.0.0.
Detects model drift via rolling metrics, CUSUM, and bucket-specific analysis.
Triggers automatic responses (light retrain, physics boost, notifications).

@zara V17.0.0
"""
from __future__ import annotations
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from ..data.db_manager import DatabaseManager
from ..core.core_helpers import get_season, SafeDateTimeUtil
from ..core.core_panel_group_forecast import get_group_capacity_kwp
from ..data.data_learning_filter import (
    HOURLY_LEARNING_ELIGIBILITY_SQL,
    PANEL_GROUP_LEARNING_ELIGIBILITY_SQL,
)

_LOGGER = logging.getLogger(__name__)
ROLLING_WINDOWS = [7, 14, 30, 60]
HOUR_BUCKETS = {
    "morning": list(range(6, 10)),
    "midday": list(range(10, 14)),
    "afternoon": list(range(14, 19)),
}
CLOUD_BUCKETS = {"clear": (0, 30), "partly_cloudy": (30, 70), "overcast": (70, 101)}
DEFAULT_CONFIG = {
    "mae_ratio_warning": 1.15,
    "mae_ratio_critical": 1.25,
    "bias_threshold": 0.15,
    "cusum_threshold": 5.0,
    "coverage_20_min": 0.6,
    "physics_boost_amount": 0.2,
    "physics_boost_max_days": 7,
}
MIN_DRIFT_STATUS_SAMPLES = 24
DRIFT_STATUS_MAX_AGE_DAYS = 2
COVERAGE_ABSOLUTE_FLOOR_KWH_PER_KWP = 0.02
MIN_COVERAGE_ABSOLUTE_FLOOR_KWH = 0.01


def _inclusive_window_start(date_str: str, window_days: int) -> str:
    """Return the first date of an inclusive fixed-length window."""
    if window_days < 1:
        raise ValueError("window_days must be positive")
    parsed_date = datetime.strptime(date_str, "%Y-%m-%d")
    return (parsed_date - timedelta(days=window_days - 1)).strftime("%Y-%m-%d")


def _within_hybrid_coverage(
    predicted_kwh: float,
    actual_kwh: float,
    relative_tolerance: float,
    capacity_kwp: float,
) -> bool:
    """Use relative coverage with a capacity-scaled low-energy floor."""
    try:
        predicted = float(predicted_kwh)
        actual = float(actual_kwh)
        tolerance = float(relative_tolerance)
        valid_capacity = float(capacity_kwp or 0.0)
    except (TypeError, ValueError):
        return False
    if not all((math.isfinite(value) for value in (predicted, actual, tolerance))):
        return False
    if not math.isfinite(valid_capacity) or valid_capacity < 0:
        valid_capacity = 0.0
    absolute_floor = max(
        MIN_COVERAGE_ABSOLUTE_FLOOR_KWH,
        valid_capacity * COVERAGE_ABSOLUTE_FLOOR_KWH_PER_KWP,
    )
    allowed_error = max(abs(actual) * max(0.0, tolerance), absolute_floor)
    return abs(predicted - actual) <= allowed_error


def _current_metric_violations(
    scope: str,
    metrics_30d: Optional[Dict[str, Any]],
    baseline_60d: Optional[Dict[str, Any]],
    config: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], bool]:
    """Evaluate current drift reasons without consulting historical events."""
    if not metrics_30d:
        return ([], True) if True else None
    try:
        sample_count = int(metrics_30d.get("sample_count") or 0)
    except (TypeError, ValueError, OverflowError):
        return ([], True) if True else None
    if sample_count < MIN_DRIFT_STATUS_SAMPLES:
        return ([], True) if True else None
    calculated_at = metrics_30d.get("calculated_at")
    if not calculated_at:
        return ([], True) if True else None
    try:
        if isinstance(calculated_at, datetime):
            metric_timestamp = calculated_at
        else:
            metric_timestamp = datetime.fromisoformat(
                str(calculated_at).replace("Z", "+00:00")
            )
        now = datetime.now(tz=metric_timestamp.tzinfo)
        metric_age = now - metric_timestamp
        if metric_age > timedelta(
            days=DRIFT_STATUS_MAX_AGE_DAYS
        ) or metric_age < timedelta(days=-1):
            return ([], True) if True else None
    except (TypeError, ValueError):
        return ([], True) if True else None
    violations = []
    try:
        mae = float(metrics_30d["mae"])
        bias = float(metrics_30d["bias"])
        coverage_20 = float(metrics_30d["coverage_20"])
    except (KeyError, TypeError, ValueError):
        return ([], True) if True else None
    if (
        not all((math.isfinite(value) for value in (mae, bias, coverage_20)))
        or mae < 0.0
        or (not 0.0 <= coverage_20 <= 1.0)
    ):
        return ([], True) if True else None
    if not baseline_60d:
        return ([], True) if True else None
    try:
        baseline_samples = int(baseline_60d.get("sample_count") or 0)
        baseline_mae = float(baseline_60d["mae"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return ([], True) if True else None
    if (
        baseline_samples < MIN_DRIFT_STATUS_SAMPLES
        or not math.isfinite(baseline_mae)
        or baseline_mae < 0.0
    ):
        return ([], True) if True else None
    baseline_calculated_at = baseline_60d.get("calculated_at")
    if not baseline_calculated_at:
        return ([], True) if True else None
    try:
        if isinstance(baseline_calculated_at, datetime):
            baseline_timestamp = baseline_calculated_at
        else:
            baseline_timestamp = datetime.fromisoformat(
                str(baseline_calculated_at).replace("Z", "+00:00")
            )
        baseline_now = datetime.now(tz=baseline_timestamp.tzinfo)
        baseline_age = baseline_now - baseline_timestamp
        if baseline_age > timedelta(
            days=DRIFT_STATUS_MAX_AGE_DAYS
        ) or baseline_age < timedelta(days=-1):
            return ([], True) if True else None
    except (TypeError, ValueError):
        return ([], True) if True else None
    if baseline_mae > 0.01:
        mae_ratio = mae / baseline_mae
        critical_threshold = float(config["mae_ratio_critical"])
        warning_threshold = float(config["mae_ratio_warning"])
        if mae_ratio > critical_threshold:
            violations.append(
                {
                    "scope": scope,
                    "drift_type": "mae_above_baseline",
                    "severity": "critical",
                    "metric_value": round(mae_ratio, 4),
                    "threshold_value": critical_threshold,
                }
            )
        elif mae_ratio > warning_threshold:
            violations.append(
                {
                    "scope": scope,
                    "drift_type": "mae_above_baseline",
                    "severity": "warning",
                    "metric_value": round(mae_ratio, 4),
                    "threshold_value": warning_threshold,
                }
            )
    bias_threshold = float(config["bias_threshold"])
    if abs(bias) > bias_threshold:
        violations.append(
            {
                "scope": scope,
                "drift_type": "bias_shift",
                "severity": "warning",
                "metric_value": round(abs(bias), 4),
                "threshold_value": bias_threshold,
            }
        )
    coverage_threshold = float(config["coverage_20_min"])
    if coverage_20 < coverage_threshold:
        violations.append(
            {
                "scope": scope,
                "drift_type": "coverage_drop",
                "severity": "warning",
                "metric_value": round(coverage_20, 4),
                "threshold_value": coverage_threshold,
            }
        )
    return (violations, False) if True else None


class CUSUMDetector:
    """Cumulative Sum detector for gradual drift in prediction residuals. @zara V17.0.0

    Lightweight algorithm suitable for Raspberry Pi.
    Detects systematic over- or under-prediction.
    """

    def __init__(
        self,
        cusum_pos: float = 0.0,
        cusum_neg: float = 0.0,
        target_mean: float = 0.0,
        alert_count: int = 0,
        bias_correction_factor: float = 1.0,
        bias_correction_updated: Optional[str] = None,
        solar_clamp_min: float = 0.75,
    ):
        self.cusum_pos = cusum_pos
        self.cusum_neg = cusum_neg
        self.target_mean = target_mean
        self.alert_count = alert_count
        self.bias_correction_factor = bias_correction_factor
        self.bias_correction_updated = bias_correction_updated
        self.solar_clamp_min = solar_clamp_min

    def update(
        self, residual: float, slack: float = 0.5, threshold: float = 5.0
    ) -> Optional[str]:
        "Update CUSUM with new residual (predicted - actual).\n\nArgs:\n    residual: predicted_kwh - actual_kwh\n    slack: Allowable deviation before accumulation\n    threshold: Alert threshold\n\nReturns:\n    'over_prediction_drift', 'under_prediction_drift', or None\n"
        deviation = residual - self.target_mean
        self.cusum_pos = max(0.0, self.cusum_pos + deviation - slack)
        self.cusum_neg = max(0.0, self.cusum_neg - deviation - slack)
        if self.cusum_pos > threshold:
            self.alert_count += 1
            return "over_prediction_drift"
        if self.cusum_neg > threshold:
            self.alert_count += 1
            return "under_prediction_drift"

    def reset(self) -> None:
        """Reset CUSUM accumulators after handling alert."""
        self.cusum_pos = 0.0
        self.cusum_neg = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cusum_pos": self.cusum_pos,
            "cusum_neg": self.cusum_neg,
            "target_mean": self.target_mean,
            "alert_count": self.alert_count,
            "bias_correction_factor": self.bias_correction_factor,
            "bias_correction_updated": self.bias_correction_updated,
            "solar_clamp_min": self.solar_clamp_min,
        }


class DriftMonitor:
    """Central drift detection and response for Solar Forecast ML. @zara V17.0.0

    Calculates rolling metrics (MAE, RMSE, Bias, Coverage) per scope/window/season.
    Detects drift via MAE ratio, bias shift, CUSUM, and coverage drop.
    Triggers automatic responses: light retrain, physics boost, notifications.
    """

    def __init__(
        self, db_manager: DatabaseManager, panel_groups: Optional[List[Any]] = None
    ):
        "Initialize drift monitor.\n\nArgs:\n    db_manager: Database manager instance\n    panel_groups: List of PanelGroup objects (may be empty)\n"
        self._db = db_manager
        self._panel_groups = panel_groups or []
        self._config = dict(DEFAULT_CONFIG)
        self._cusum_detectors = {}
        self._physics_boost = {}
        self._initialized = False

    async def initialize(self) -> None:
        """Load config and CUSUM state from database."""
        if self._initialized:
            return
        try:
            self._config = dict(DEFAULT_CONFIG)
            for scope in self._get_scopes():
                stored_state = await self._db.load_drift_cusum_state(scope)
                if stored_state:
                    self._cusum_detectors[scope] = CUSUMDetector(
                        cusum_pos=stored_state.get("cusum_pos", 0.0),
                        cusum_neg=stored_state.get("cusum_neg", 0.0),
                        target_mean=stored_state.get("target_mean", 0.0),
                        alert_count=stored_state.get("alert_count", 0),
                        bias_correction_factor=stored_state.get(
                            "bias_correction_factor", 1.0
                        ),
                        bias_correction_updated=stored_state.get(
                            "bias_correction_updated"
                        ),
                        solar_clamp_min=stored_state.get("solar_clamp_min", 0.75),
                    )
                else:
                    self._cusum_detectors[scope] = CUSUMDetector()
            for scope in self._get_scopes():
                detector = self._cusum_detectors[scope]
                if detector.target_mean == 0.0:
                    initial_target = await self._auto_initialize_cusum_target(scope)
                    if initial_target != 0.0:
                        detector.target_mean = initial_target
                        await self._db.save_drift_cusum_state(scope, detector.to_dict())
                        _LOGGER.info(
                            "CUSUM target_mean auto-initialized: scope=%s, target=%.4f",
                            scope,
                            initial_target,
                        )
            self._initialized = True
            _LOGGER.debug(
                "DriftMonitor initialized with %d scopes", len(self._get_scopes())
            )
        except Exception as error:
            _LOGGER.error("DriftMonitor initialization failed: %s", error)
            self._initialized = True
        return None if True else None

    def _get_scopes(self) -> List[str]:
        """Get all monitoring scopes: 'global' + panel group names."""
        scopes = ["global"]
        for panel_group in self._panel_groups:
            scopes.append(
                panel_group.get("name", "unknown")
                if isinstance(panel_group, dict)
                else panel_group.name
            )
        return scopes

    def _scope_capacity_kwp(self, scope: str) -> float:
        """Return configured capacity for global or one panel-group scope."""
        capacities = []
        for panel_group in self._panel_groups:
            group_name = (
                panel_group.get("name", "unknown")
                if isinstance(panel_group, dict)
                else getattr(panel_group, "name", "unknown")
            )
            valid_capacity = get_group_capacity_kwp(panel_group)
            if math.isfinite(valid_capacity) and valid_capacity > 0:
                capacities.append((group_name, valid_capacity))
        if scope == "global":
            return sum(
                (valid_capacity for unused_group_name, valid_capacity in capacities)
            )
        return next(
            (
                valid_capacity
                for group_name, valid_capacity in capacities
                if group_name == scope
            ),
            0.0,
        )

    async def calculate_rolling_metrics(self, date_str: str) -> Dict[str, Any]:
        """Calculate rolling-window metrics for all scopes and windows. @zara V17.0.0

        Called by production_scheduled_tasks as EOD Step 14.

        Args:
            date_str: Date string YYYY-MM-DD

        Returns:
            Dict with calculation summary
        """
        await self.initialize()
        month = datetime.strptime(date_str, "%Y-%m-%d").month
        season = get_season(month)
        scopes = self._get_scopes()
        metrics_saved = 0
        events_created = 0
        metric_failures = []
        operation_failures = []
        fresh_metrics = {}
        for scope in scopes:
            for window_days in ROLLING_WINDOWS:
                for metric_season in (None, season):
                    try:
                        metrics = await self._compute_rolling_metrics(
                            scope, window_days, date_str, metric_season
                        )
                        if metrics:
                            if metrics.get("sample_count", 0) >= 3:
                                await self._db.save_drift_rolling_metric(
                                    scope, window_days, metric_season, metrics
                                )
                                fresh_metrics[scope, window_days, metric_season] = (
                                    metrics
                                )
                                metrics_saved += 1
                    except Exception as error:
                        metric_failures.append(f"{scope}/{window_days}/{metric_season}")
                        _LOGGER.debug(
                            "Rolling metric %s/%d/%s failed: %s",
                            scope,
                            window_days,
                            metric_season,
                            error,
                        )
                        continue
            try:
                bucket_events = await self._compute_bucket_metrics(
                    scope, date_str, season
                )
                events_created += bucket_events
            except Exception as error:
                operation_failures.append(f"bucket/{scope}")
                _LOGGER.error("Bucket metrics for %s failed: %s", scope, error)
                continue
        try:
            cusum_events = await self._run_cusum_detection(date_str)
            events_created += cusum_events
        except Exception as error:
            operation_failures.append("cusum")
            _LOGGER.error("CUSUM detection failed: %s", error)
        if metric_failures:
            _LOGGER.error(
                "Drift rolling metrics incomplete: %d calculations failed (%s)",
                len(metric_failures),
                ", ".join(metric_failures),
            )
        elif not operation_failures:
            try:
                response_events = await self._check_and_respond_to_drift(
                    date_str, fresh_metrics
                )
                events_created += response_events
            except Exception as error:
                operation_failures.append("response")
                _LOGGER.error("Drift response check failed: %s", error)
        global_bias_factor = 1.0
        global_bias_fresh = False
        global_clamp_fresh = False
        if not metric_failures and (not operation_failures):
            for scope in scopes:
                metrics_7d = fresh_metrics.get((scope, 7, None))
                metrics_14d = fresh_metrics.get((scope, 14, None))
                if not metrics_7d or not metrics_14d:
                    continue
                try:
                    bias_factor = await self._compute_and_apply_bias_correction(
                        scope, date_str, metrics_7d, metrics_14d
                    )
                except Exception as error:
                    operation_failures.append(f"bias/{scope}")
                    _LOGGER.error("Bias correction for %s failed: %s", scope, error)
                    continue
                if bias_factor is None:
                    continue
                if scope == "global":
                    global_bias_factor = bias_factor
                    global_bias_fresh = True
                    try:
                        global_clamp_fresh = await self._update_solar_clamp("global")
                    except Exception as error:
                        operation_failures.append("solar_clamp/global")
                        _LOGGER.error("Solar clamp update failed: %s", error)
                        continue
            if not operation_failures:
                state_failures = []
                for scope in scopes:
                    detector = self._cusum_detectors.get(scope)
                    if detector:
                        try:
                            await self._db.save_drift_cusum_state(
                                scope, detector.to_dict()
                            )
                        except Exception as error:
                            state_failures.append(scope)
                            _LOGGER.error(
                                "Drift state persistence for %s failed: %s",
                                scope,
                                error,
                            )
                            continue
                operation_failures.extend(
                    (f"state/{scope}" for scope in state_failures)
                )
        if operation_failures:
            _LOGGER.error(
                "Drift adaptive operations incomplete: %d failed (%s)",
                len(operation_failures),
                ", ".join(operation_failures),
            )
        _LOGGER.info(
            "Drift detection complete: %d metrics saved, %d events created",
            metrics_saved,
            events_created,
        )
        return {
            "metrics_saved": metrics_saved,
            "metrics_failed": len(metric_failures),
            "operations_failed": len(operation_failures),
            "events_created": events_created,
            "scopes_checked": len(scopes),
            "bias_correction_global": round(global_bias_factor, 4),
            "global_bias_fresh": global_bias_fresh,
            "global_clamp_fresh": global_clamp_fresh,
        }

    async def _auto_initialize_cusum_target(self, scope: str) -> float:
        """Compute trimmed mean of 30-day daily residuals for CUSUM baseline. @zara V17.2.0"""
        try:
            end_date = SafeDateTimeUtil.now().strftime("%Y-%m-%d")
            start_date = _inclusive_window_start(end_date, 30)
            if scope == "global":
                rows = await self._db.fetchall(
                    f"SELECT AVG(hp.prediction_kwh - hp.actual_kwh) as daily_res\n                       FROM hourly_predictions hp\n                       WHERE hp.target_date >= ? AND hp.target_date <= ?\n                           AND hp.actual_kwh IS NOT NULL AND hp.actual_kwh > 0.01\n                           AND hp.prediction_kwh IS NOT NULL\n                           {HOURLY_LEARNING_ELIGIBILITY_SQL}\n                       GROUP BY target_date\n                       HAVING COUNT(*) >= 3",
                    (start_date, end_date),
                )
            else:
                rows = await self._db.fetchall(
                    f"SELECT AVG(ppg.prediction_kwh - ppg.actual_kwh) as daily_res\n                       FROM prediction_panel_groups ppg\n                       JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                       WHERE hp.target_date >= ? AND hp.target_date <= ?\n                           AND ppg.actual_kwh IS NOT NULL AND ppg.actual_kwh > 0.01\n                           AND ppg.prediction_kwh IS NOT NULL\n                           AND ppg.group_name = ?\n                           {PANEL_GROUP_LEARNING_ELIGIBILITY_SQL}\n                       GROUP BY hp.target_date\n                       HAVING COUNT(*) >= 3",
                    (start_date, end_date, scope),
                )
            if not rows:
                return 0.0
            pass
            sorted_residuals = sorted(
                [float(row[0]) for row in rows if row[0] is not None]
            )
            residual_count = len(sorted_residuals)
            if residual_count < 7:
                return 0.0
            trim_count = max(1, int(residual_count * 0.1))
            trimmed_residuals = (
                sorted_residuals[trim_count:-trim_count]
                if trim_count > 0
                else sorted_residuals
            )
            if not trimmed_residuals:
                return 0.0
            target_mean = sum(trimmed_residuals) / len(trimmed_residuals)
            _LOGGER.info(
                "CUSUM target_mean auto-initialized: scope=%s, n_days=%d, target=%.4f",
                scope,
                residual_count,
                target_mean,
            )
            return round(target_mean, 4)
        except Exception as error:
            _LOGGER.debug("CUSUM auto-init for %s failed: %s", scope, error)
            return 0.0

    async def _compute_and_apply_bias_correction(
        self,
        scope: str,
        date_str: str,
        metrics_7d: Dict[str, Any],
        metrics_14d: Dict[str, Any],
    ) -> Optional[float]:
        """EMA bias correction factor from 7d/14d rolling bias. @zara V17.2.0

        Returns the new bias_correction_factor (1.0 = no correction).
        """
        try:
            combined_sample_count = (metrics_7d.get("sample_count", 0) or 0) + (
                metrics_14d.get("sample_count", 0) or 0
            )
            if combined_sample_count < 56:
                return
            bias_7d = metrics_7d.get("bias", 0.0) or 0.0
            bias_14d = metrics_14d.get("bias", 0.0) or 0.0
            weighted_bias = 0.7 * bias_7d + 0.3 * bias_14d
            bias_window_start = _inclusive_window_start(date_str, 14)
            if scope == "global":
                actual_mean_row = await self._db.fetchone(
                    f"SELECT AVG(hp.actual_kwh) FROM hourly_predictions hp\n                        WHERE hp.target_date >= ? AND hp.target_date <= ?\n                            AND hp.actual_kwh > 0.01\n                            {HOURLY_LEARNING_ELIGIBILITY_SQL}",
                    (bias_window_start, date_str),
                )
            else:
                actual_mean_row = await self._db.fetchone(
                    f"SELECT AVG(ppg.actual_kwh) FROM prediction_panel_groups ppg\n                       JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                       WHERE hp.target_date >= ? AND hp.target_date <= ?\n                           AND ppg.actual_kwh > 0.01 AND ppg.group_name = ?\n                           {PANEL_GROUP_LEARNING_ELIGIBILITY_SQL}",
                    (bias_window_start, date_str, scope),
                )
            mean_actual = (
                float(actual_mean_row[0])
                if actual_mean_row and actual_mean_row[0] is not None
                else 0.0
            )
            if mean_actual < 0.05:
                return
            relative_bias = weighted_bias / mean_actual
            detector = self._cusum_detectors.get(scope, CUSUMDetector())
            previous_factor = detector.bias_correction_factor
            if abs(relative_bias) < 0.05:
                target_factor = 1.0
            else:
                target_factor = 1.0 / (1.0 + relative_bias)
                target_factor = max(0.85, min(1.15, target_factor))
            smoothed_factor = 0.15 * target_factor + 0.85 * previous_factor
            smoothed_factor = max(0.85, min(1.15, smoothed_factor))
            if abs(smoothed_factor - previous_factor) > 0.005:
                _LOGGER.info(
                    "Drift bias correction updated: %.4f → %.4f (scope=%s, rel_bias=%.3f)",
                    previous_factor,
                    smoothed_factor,
                    scope,
                    relative_bias,
                )
            detector.bias_correction_factor = round(smoothed_factor, 4)
            detector.bias_correction_updated = SafeDateTimeUtil.now().isoformat()
            self._cusum_detectors[scope] = detector
            return smoothed_factor
        except Exception:
            raise

    async def _update_solar_clamp(self, scope: str) -> bool:
        """Relax solar clamp min if systematic over-forecast and precision confident. @zara V17.2.0"""
        if scope != "global":
            return False
        try:
            precision_row = await self._db.fetchone(
                "SELECT solar_factor, confidence FROM weather_precision_factors ORDER BY updated_at DESC LIMIT 1"
            )
            if not precision_row or precision_row[0] is None:
                return False
            solar_factor = float(precision_row[0])
            confidence = (
                float(precision_row[1]) if precision_row[1] is not None else 0.0
            )
            detector = self._cusum_detectors.get("global", CUSUMDetector())
            bias_factor = detector.bias_correction_factor
            previous_clamp = detector.solar_clamp_min
            precision_confident = confidence >= 0.65
            solar_overforecast = solar_factor < 0.75
            bias_overforecast = bias_factor < 0.95
            if precision_confident and solar_overforecast and bias_overforecast:
                relaxed_clamp = max(0.65, min(0.75, solar_factor + 0.05))
                if relaxed_clamp < previous_clamp - 0.001:
                    _LOGGER.info(
                        "Solar clamp relaxed: %.3f → %.3f (solar_factor=%.2f, confidence=%.2f, bias_cf=%.4f)",
                        previous_clamp,
                        relaxed_clamp,
                        solar_factor,
                        confidence,
                        bias_factor,
                    )
                detector.solar_clamp_min = round(relaxed_clamp, 3)
            else:
                detector.solar_clamp_min = 0.75
            self._cusum_detectors["global"] = detector
        except Exception:
            raise
        return True

    def get_global_bias_correction(self) -> float:
        """Current EMA bias correction factor for global scope. Default 1.0. @zara V17.2.0"""
        detector = self._cusum_detectors.get("global")
        return detector.bias_correction_factor if detector else 1.0

    def get_solar_clamp_min(self) -> float:
        """Current adaptive solar clamp minimum. Default 0.75. @zara V17.2.0"""
        detector = self._cusum_detectors.get("global")
        return detector.solar_clamp_min if detector else 0.75

    async def _compute_rolling_metrics(
        self, scope: str, window_days: int, date_str: str, season: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Compute MAE, RMSE, Bias, Coverage for a scope/window/season."""
        end_date = date_str
        start_date = _inclusive_window_start(date_str, window_days)
        if scope == "global":
            query = f"\n                SELECT hp.prediction_kwh, hp.actual_kwh\n                FROM hourly_predictions hp\n                WHERE hp.target_date >= ? AND hp.target_date <= ?\n                    AND hp.actual_kwh IS NOT NULL\n                    AND hp.prediction_kwh IS NOT NULL\n                    AND hp.actual_kwh > 0.01\n                    {HOURLY_LEARNING_ELIGIBILITY_SQL}"
            params = [start_date, end_date]
        else:
            query = f"\n                SELECT ppg.prediction_kwh, ppg.actual_kwh\n                FROM prediction_panel_groups ppg\n                JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                WHERE hp.target_date >= ? AND hp.target_date <= ?\n                    AND ppg.actual_kwh IS NOT NULL\n                    AND ppg.prediction_kwh IS NOT NULL\n                    AND ppg.actual_kwh > 0.01\n                    AND ppg.group_name = ?\n                    {PANEL_GROUP_LEARNING_ELIGIBILITY_SQL}"
            params = [start_date, end_date, scope]
        if season:
            season_months = _get_season_months(season)
            placeholders = ",".join("?" * len(season_months))
            query += f"\n                    AND CAST(strftime('%m', hp.target_date) AS INTEGER) IN ({placeholders})"
            params.extend(season_months)
        rows = await self._db.fetchall(query, tuple(params))
        if not rows or len(rows) < 3:
            return None
        predictions = [float(row[0]) for row in rows]
        actuals = [float(row[1]) for row in rows]
        sample_count = len(predictions)
        residuals = [
            predicted - actual for predicted, actual in zip(predictions, actuals)
        ]
        absolute_errors = [abs(residual) for residual in residuals]
        mae = sum(absolute_errors) / sample_count
        rmse = math.sqrt(sum((residual**2 for residual in residuals)) / sample_count)
        bias = sum(residuals) / sample_count
        scope_capacity = self._scope_capacity_kwp(scope)
        coverage_10 = (
            sum(
                (
                    1
                    for predicted, actual in zip(predictions, actuals)
                    if _within_hybrid_coverage(predicted, actual, 0.1, scope_capacity)
                )
            )
            / sample_count
        )
        coverage_20 = (
            sum(
                (
                    1
                    for predicted, actual in zip(predictions, actuals)
                    if _within_hybrid_coverage(predicted, actual, 0.2, scope_capacity)
                )
            )
            / sample_count
        )
        return {
            "mae": round(mae, 4),
            "rmse": round(rmse, 4),
            "bias": round(bias, 4),
            "coverage_10": round(coverage_10, 3),
            "coverage_20": round(coverage_20, 3),
            "sample_count": sample_count,
        }

    async def _compute_bucket_metrics(
        self, scope: str, date_str: str, current_season: str
    ) -> int:
        """Compute bucket-specific drift metrics (cloud x hour x season). Returns event count."""
        events_created = 0
        end_date = date_str
        recent_start = _inclusive_window_start(date_str, 30)
        baseline_start = _inclusive_window_start(date_str, 90)
        for cloud_bucket, (cloud_min, cloud_max) in CLOUD_BUCKETS.items():
            for hour_bucket, bucket_hours in HOUR_BUCKETS.items():
                hour_placeholders = ",".join("?" * len(bucket_hours))
                for metric_season in (None, current_season):
                    try:
                        recent_bucket = await self._bucket_mae(
                            scope,
                            recent_start,
                            end_date,
                            cloud_min,
                            cloud_max,
                            bucket_hours,
                            hour_placeholders,
                            metric_season,
                        )
                        baseline_bucket = await self._bucket_mae(
                            scope,
                            baseline_start,
                            end_date,
                            cloud_min,
                            cloud_max,
                            bucket_hours,
                            hour_placeholders,
                            metric_season,
                        )
                        if recent_bucket is None or baseline_bucket is None:
                            continue
                        recent_mae, recent_count = recent_bucket
                        baseline_mae, baseline_count = baseline_bucket
                        if baseline_mae < 0.01:
                            continue
                        mae_ratio = recent_mae / baseline_mae
                        await self._db.save_drift_bucket_metric(
                            scope,
                            cloud_bucket,
                            hour_bucket,
                            metric_season,
                            {
                                "mae": recent_mae,
                                "mae_baseline": baseline_mae,
                                "mae_ratio": round(mae_ratio, 3),
                                "bias": 0.0,
                                "sample_count": recent_count,
                            },
                        )
                        if mae_ratio > 1.2:
                            await self._create_drift_event(
                                date_str,
                                scope,
                                "mae_above_baseline",
                                "info",
                                f"{cloud_bucket}/{hour_bucket}",
                                mae_ratio,
                                1.2,
                                f"Bucket {cloud_bucket}/{hour_bucket} MAE ratio {mae_ratio:.2f} (season={metric_season})",
                                "none",
                            )
                            events_created += 1
                    except Exception as error:
                        _LOGGER.debug(
                            "Bucket metric %s/%s/%s failed: %s",
                            cloud_bucket,
                            hour_bucket,
                            metric_season,
                            error,
                        )
                        raise
                continue
            continue
        return events_created

    async def _bucket_mae(
        self,
        scope: str,
        start_date: str,
        end_date: str,
        cloud_min: int,
        cloud_max: int,
        hours: List[int],
        hour_placeholders: str,
        season: Optional[str] = None,
    ) -> Optional[Tuple[float, int]]:
        """Calculate MAE and sample count for a specific bucket."""
        if scope == "global":
            query = f"\n                SELECT hp.prediction_kwh, hp.actual_kwh\n                FROM hourly_predictions hp\n                LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                    AND pw.weather_type = 'forecast'\n                WHERE hp.target_date >= ? AND hp.target_date <= ?\n                    AND hp.actual_kwh IS NOT NULL AND hp.actual_kwh > 0.01\n                    AND hp.prediction_kwh IS NOT NULL\n                    {HOURLY_LEARNING_ELIGIBILITY_SQL}\n                    AND hp.target_hour IN ({hour_placeholders})\n                    AND COALESCE(pw.clouds, 50) >= ? AND COALESCE(pw.clouds, 50) < ?"
            params = [start_date, end_date] + hours + [cloud_min, cloud_max]
        else:
            query = f"\n                SELECT ppg.prediction_kwh, ppg.actual_kwh\n                FROM prediction_panel_groups ppg\n                JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                    AND pw.weather_type = 'forecast'\n                WHERE hp.target_date >= ? AND hp.target_date <= ?\n                    AND ppg.actual_kwh IS NOT NULL AND ppg.actual_kwh > 0.01\n                    AND ppg.prediction_kwh IS NOT NULL\n                    AND ppg.group_name = ?\n                    {PANEL_GROUP_LEARNING_ELIGIBILITY_SQL}\n                    AND hp.target_hour IN ({hour_placeholders})\n                    AND COALESCE(pw.clouds, 50) >= ? AND COALESCE(pw.clouds, 50) < ?"
            params = [start_date, end_date, scope] + hours + [cloud_min, cloud_max]
        if season:
            season_months = _get_season_months(season)
            placeholders = ",".join("?" * len(season_months))
            query += f"\n                    AND CAST(strftime('%m', hp.target_date) AS INTEGER) IN ({placeholders})"
            params.extend(season_months)
        rows = await self._db.fetchall(query, tuple(params))
        if not rows or len(rows) < 3:
            return None
        mae = sum((abs(float(row[0]) - float(row[1])) for row in rows)) / len(rows)
        return (mae, len(rows))

    async def _run_cusum_detection(self, date_str: str) -> int:
        """Run CUSUM detection for today's residuals. Returns event count."""
        events_created = 0
        threshold = self._config.get("cusum_threshold", 5.0)
        for scope in self._get_scopes():
            try:
                if scope == "global":
                    rows = await self._db.fetchall(
                        f"SELECT hp.prediction_kwh, hp.actual_kwh\n                           FROM hourly_predictions hp\n                           WHERE hp.target_date = ?\n                               AND hp.actual_kwh IS NOT NULL AND hp.actual_kwh > 0.01\n                               AND hp.prediction_kwh IS NOT NULL\n                               {HOURLY_LEARNING_ELIGIBILITY_SQL}",
                        (date_str,),
                    )
                else:
                    rows = await self._db.fetchall(
                        f"SELECT ppg.prediction_kwh, ppg.actual_kwh\n                           FROM prediction_panel_groups ppg\n                           JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                           WHERE hp.target_date = ? AND ppg.group_name = ?\n                               AND ppg.actual_kwh IS NOT NULL AND ppg.actual_kwh > 0.01\n                               AND ppg.prediction_kwh IS NOT NULL\n                               {PANEL_GROUP_LEARNING_ELIGIBILITY_SQL}",
                        (date_str, scope),
                    )
                if not rows:
                    continue
                detector = self._cusum_detectors.get(scope, CUSUMDetector())
                for row in rows:
                    residual = float(row[0]) - float(row[1])
                    alert = detector.update(residual, threshold=threshold)
                    if alert:
                        severity = "critical"
                        cusum_value = max(detector.cusum_pos, detector.cusum_neg)
                        await self._create_drift_event(
                            date_str,
                            scope,
                            "cusum_alert",
                            severity,
                            None,
                            cusum_value,
                            threshold,
                            f"CUSUM alert: {alert} (scope={scope}, value={cusum_value:.2f})",
                            "drift_evidence",
                        )
                        detector.reset()
                        events_created += 1
                await self._db.save_drift_cusum_state(scope, detector.to_dict())
                self._cusum_detectors[scope] = detector
            except Exception as error:
                _LOGGER.debug("CUSUM for scope %s failed: %s", scope, error)
                raise
            continue
        return events_created

    async def _create_drift_event(
        self,
        date_str: str,
        scope: str,
        drift_type: str,
        severity: str,
        bucket_detail: Optional[str],
        metric_value: float,
        threshold_value: float,
        description: str,
        response_action: str,
    ) -> None:
        """Create a drift event record."""
        await self._db.save_drift_event(
            {
                "event_date": date_str,
                "scope": scope,
                "drift_type": drift_type,
                "severity": severity,
                "bucket_detail": bucket_detail,
                "metric_value": round(metric_value, 4),
                "threshold_value": round(threshold_value, 4),
                "description": description,
                "response_action": response_action,
            }
        )

    async def _check_and_respond_to_drift(
        self,
        date_str: str,
        fresh_metrics: Dict[Tuple[str, int, Optional[str]], Dict[str, Any]],
    ) -> int:
        """Check fresh rolling metrics for drift triggers and execute responses."""
        events_created = 0
        for scope in self._get_scopes():
            try:
                metrics = fresh_metrics.get((scope, 30, None))
                if not metrics:
                    continue
                mae = metrics.get("mae", 0)
                bias = metrics.get("bias", 0)
                coverage_20 = metrics.get("coverage_20", 1.0)
                baseline_metrics = fresh_metrics.get((scope, 60, None))
                baseline_mae = baseline_metrics.get("mae", 0) if baseline_metrics else 0
                if baseline_mae > 0.01:
                    mae_ratio = mae / baseline_mae
                    critical_threshold = self._config["mae_ratio_critical"]
                    warning_threshold = self._config["mae_ratio_warning"]
                    if mae_ratio > critical_threshold:
                        await self._create_drift_event(
                            date_str,
                            scope,
                            "mae_above_baseline",
                            "critical",
                            None,
                            mae_ratio,
                            critical_threshold,
                            f"MAE ratio {mae_ratio:.2f} exceeds critical threshold (scope={scope})",
                            "light_retrain",
                        )
                        events_created += 1
                        await self._activate_physics_boost(scope)
                    elif mae_ratio > warning_threshold:
                        await self._create_drift_event(
                            date_str,
                            scope,
                            "mae_above_baseline",
                            "warning",
                            None,
                            mae_ratio,
                            warning_threshold,
                            f"MAE ratio {mae_ratio:.2f} exceeds warning threshold (scope={scope})",
                            "light_retrain",
                        )
                        events_created += 1
                bias_threshold = self._config["bias_threshold"]
                if abs(bias) > bias_threshold:
                    await self._create_drift_event(
                        date_str,
                        scope,
                        "bias_shift",
                        "warning",
                        None,
                        abs(bias),
                        bias_threshold,
                        f"Systematic bias {bias:+.3f} kWh (scope={scope})",
                        "bias_correction",
                    )
                    events_created += 1
                coverage_threshold = self._config["coverage_20_min"]
                if coverage_20 < coverage_threshold:
                    await self._create_drift_event(
                        date_str,
                        scope,
                        "coverage_drop",
                        "warning",
                        None,
                        coverage_20,
                        coverage_threshold,
                        f"Coverage-20 dropped to {coverage_20:.0%} (scope={scope})",
                        "data_quality_guard",
                    )
                    events_created += 1
                    continue
            except Exception as error:
                _LOGGER.debug("Drift response check for %s failed: %s", scope, error)
                raise
        return events_created

    async def _activate_physics_boost(self, scope: str) -> None:
        """Activate temporary physics boost for a scope."""
        boost_amount = self._config["physics_boost_amount"]
        max_days = self._config["physics_boost_max_days"]
        self._physics_boost[scope] = {
            "boost": boost_amount,
            "activated_at": SafeDateTimeUtil.now().isoformat(),
            "max_days": max_days,
        }
        _LOGGER.warning(
            "Physics boost activated: scope=%s, boost=%.0f%%, max_days=%d",
            scope,
            boost_amount * 100,
            max_days,
        )

    def get_active_physics_boost(self, scope: str) -> float:
        """Get active physics boost for a scope. Returns 0.0 if no boost active.

        Used by forecast_rule_based_strategy._blend_hour() to reduce AI confidence.
        """
        boost_state = self._physics_boost.get(scope)
        if not boost_state:
            boost_state = self._physics_boost.get("global")
        if not boost_state:
            return 0.0
        try:
            activated_at = SafeDateTimeUtil.ensure_local(
                datetime.fromisoformat(boost_state["activated_at"])
            )
            max_days = boost_state.get("max_days", 7)
            if (SafeDateTimeUtil.now() - activated_at).days >= max_days:
                self._physics_boost.pop(scope, None)
                return 0.0
            return boost_state.get("boost", 0.0)
        except (ValueError, KeyError):
            return 0.0

    async def get_status(self) -> Dict[str, Any]:
        """Get drift status derived from current rolling metrics."""
        await self.initialize()
        try:
            global_metrics_7d = await self._db.get_drift_rolling_metrics(
                "global", 7, None
            )
            global_metrics_30d = await self._db.get_drift_rolling_metrics(
                "global", 30, None
            )
            global_baseline_60d = await self._db.get_drift_rolling_metrics(
                "global", 60, None
            )
            active_reasons, global_insufficient = _current_metric_violations(
                "global", global_metrics_30d, global_baseline_60d, self._config
            )
            insufficient_scopes = ["global"] if global_insufficient else []
            per_group_status = {}
            for panel_group in self._panel_groups:
                group_name = (
                    panel_group.get("name", "unknown")
                    if isinstance(panel_group, dict)
                    else panel_group.name
                )
                group_metrics_30d = await self._db.get_drift_rolling_metrics(
                    group_name, 30, None
                )
                group_baseline_60d = await self._db.get_drift_rolling_metrics(
                    group_name, 60, None
                )
                group_reasons, group_insufficient = _current_metric_violations(
                    group_name, group_metrics_30d, group_baseline_60d, self._config
                )
                active_reasons.extend(group_reasons)
                if group_insufficient:
                    insufficient_scopes.append(group_name)
                group_state = "insufficient_data" if group_insufficient else "stable"
                if any((reason["severity"] == "critical" for reason in group_reasons)):
                    group_state = "critical"
                elif group_reasons:
                    group_state = "warning"
                per_group_status[group_name] = {
                    "mae_30d": (
                        group_metrics_30d.get("mae") if group_metrics_30d else None
                    ),
                    "bias_30d": (
                        group_metrics_30d.get("bias") if group_metrics_30d else None
                    ),
                    "drift": group_state,
                }
                continue
            overall_state = "stable"
            if any((reason["severity"] == "critical" for reason in active_reasons)):
                overall_state = "critical"
            elif active_reasons:
                overall_state = "warning"
            elif global_insufficient:
                overall_state = "insufficient_data"
            physics_boost_active = any(
                (
                    self.get_active_physics_boost(scope) > 0
                    for scope in self._get_scopes()
                )
            )
            global_detector = self._cusum_detectors.get("global", CUSUMDetector())
            return {
                "state": overall_state,
                "global_mae_7d": (
                    global_metrics_7d.get("mae") if global_metrics_7d else None
                ),
                "global_mae_30d": (
                    global_metrics_30d.get("mae") if global_metrics_30d else None
                ),
                "global_bias_30d": (
                    global_metrics_30d.get("bias") if global_metrics_30d else None
                ),
                "global_coverage_20_30d": (
                    global_metrics_30d.get("coverage_20")
                    if global_metrics_30d
                    else None
                ),
                "active_drift_events": len(active_reasons),
                "active_drift_reasons": active_reasons,
                "status_basis": "current_30d_vs_60d_metrics",
                "insufficient_scopes": insufficient_scopes,
                "physics_boost_active": physics_boost_active,
                "per_group_status": per_group_status,
                "bias_correction_factor": global_detector.bias_correction_factor,
                "solar_clamp_min": global_detector.solar_clamp_min,
                "cusum_target_mean_global": global_detector.target_mean,
                "last_check": SafeDateTimeUtil.now().isoformat(),
            }
        except Exception as error:
            _LOGGER.error("Failed to get drift status: %s", error)
            return {"state": "unknown", "error": str(error)}


def _get_season_months(season: str) -> List[int]:
    """Get month numbers for a season."""
    season_months = {
        "winter": [12, 1, 2],
        "spring": [3, 4, 5],
        "summer": [6, 7, 8],
        "autumn": [9, 10, 11],
    }
    return season_months.get(season, [])
