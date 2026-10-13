# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Drift Monitor for Solar Forecast Energy AI V17.0.0.
Detects model drift via rolling metrics, CUSUM, and bucket-specific analysis.
Triggers automatic responses (light retrain, physics boost, notifications).

@zara V17.0.0
"""
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from ..data.db_manager import DatabaseManager
from ..core.core_helpers import get_season

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
    ):
        self.cusum_pos = cusum_pos
        self.cusum_neg = cusum_neg
        self.target_mean = target_mean
        self.alert_count = alert_count

    def update(
        self, residual: float, slack: float = 0.5, threshold: float = 5.0
    ) -> Optional[str]:
        "Update CUSUM with new residual (predicted - actual).\n\nArgs:\n    residual: predicted_kwh - actual_kwh\n    slack: Allowable deviation before accumulation\n    threshold: Alert threshold\n\nReturns:\n    'over_prediction_drift', 'under_prediction_drift', or None\n"
        centered_residual = residual - self.target_mean
        self.cusum_pos = max(0.0, self.cusum_pos + centered_residual - slack)
        self.cusum_neg = max(0.0, self.cusum_neg - centered_residual - slack)
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
        }


class DriftMonitor:
    """Central drift detection and response for Solar Forecast Energy AI. @zara V17.0.0

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
            stored_config = await self._db.load_drift_response_config()
            if stored_config:
                self._config.update(stored_config)
            for scope_name in self._get_scopes():
                cusum_state = await self._db.load_drift_cusum_state(scope_name)
                if cusum_state:
                    self._cusum_detectors[scope_name] = CUSUMDetector(
                        cusum_pos=cusum_state.get("cusum_pos", 0.0),
                        cusum_neg=cusum_state.get("cusum_neg", 0.0),
                        target_mean=cusum_state.get("target_mean", 0.0),
                        alert_count=cusum_state.get("alert_count", 0),
                    )
                else:
                    self._cusum_detectors[scope_name] = CUSUMDetector()
            self._initialized = True
            _LOGGER.info(
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

    async def calculate_rolling_metrics(self, date_str: str) -> Dict[str, Any]:
        "Calculate rolling-window metrics for all scopes and windows. @zara V17.0.0\n\nCalled by consumption_scheduled_tasks as EOD Step 14.\n\nArgs:\n    date_str: Date string YYYY-MM-DD\n\nReturns:\n    Dict with calculation summary\n"
        await self.initialize()
        forecast_month = datetime.strptime(date_str, "%Y-%m-%d").month
        current_season = get_season(forecast_month)
        scopes = self._get_scopes()
        metrics_saved = 0
        events_created = 0
        for scope_name in scopes:
            for rolling_window_days in ROLLING_WINDOWS:
                for season_filter in (None, current_season):
                    try:
                        rolling_metrics = await self._compute_rolling_metrics(
                            scope_name, rolling_window_days, date_str, season_filter
                        )
                        if rolling_metrics:
                            if rolling_metrics.get("sample_count", 0) >= 3:
                                await self._db.save_drift_rolling_metric(
                                    scope_name,
                                    rolling_window_days,
                                    season_filter,
                                    rolling_metrics,
                                )
                                metrics_saved += 1
                    except Exception as error:
                        _LOGGER.debug(
                            "Rolling metric %s/%d/%s failed: %s",
                            scope_name,
                            rolling_window_days,
                            season_filter,
                            error,
                        )
                        continue
            try:
                bucket_event_count = await self._compute_bucket_metrics(
                    scope_name, date_str, current_season
                )
                events_created += bucket_event_count
            except Exception as error:
                _LOGGER.debug("Bucket metrics for %s failed: %s", scope_name, error)
                continue
        try:
            cusum_event_count = await self._run_cusum_detection(date_str)
            events_created += cusum_event_count
        except Exception as error:
            _LOGGER.debug("CUSUM detection failed: %s", error)
        try:
            await self._check_and_respond_to_drift(date_str)
        except Exception as error:
            _LOGGER.debug("Drift response check failed: %s", error)
        _LOGGER.info(
            "Drift detection complete: %d metrics saved, %d events created",
            metrics_saved,
            events_created,
        )
        return {
            "metrics_saved": metrics_saved,
            "events_created": events_created,
            "scopes_checked": len(scopes),
        }

    async def _compute_rolling_metrics(
        self, scope: str, window_days: int, date_str: str, season: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Compute MAE, RMSE, Bias, Coverage for a scope/window/season."""
        end_date = date_str
        start_date = (
            datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=window_days)
        ).strftime("%Y-%m-%d")
        if scope == "global":
            sql = "\n                SELECT hp.prediction_kwh, hp.actual_kwh\n                FROM hourly_predictions hp\n                WHERE hp.target_date >= ? AND hp.target_date <= ?\n                    AND hp.actual_kwh IS NOT NULL\n                    AND hp.prediction_kwh IS NOT NULL\n                    AND hp.actual_kwh > 0.01"
            query_parameters = [start_date, end_date]
        else:
            sql = "\n                SELECT ppg.prediction_kwh, ppg.actual_kwh\n                FROM prediction_panel_groups ppg\n                JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                WHERE hp.target_date >= ? AND hp.target_date <= ?\n                    AND ppg.actual_kwh IS NOT NULL\n                    AND ppg.prediction_kwh IS NOT NULL\n                    AND ppg.actual_kwh > 0.01\n                    AND ppg.group_name = ?"
            query_parameters = [start_date, end_date, scope]
        if season:
            season_months = _get_season_months(season)
            season_placeholders = ",".join("?" * len(season_months))
            sql += f"\n                    AND CAST(strftime('%m', hp.target_date) AS INTEGER) IN ({season_placeholders})"
            query_parameters.extend(season_months)
        prediction_rows = await self._db.fetchall(sql, tuple(query_parameters))
        if not prediction_rows or len(prediction_rows) < 3:
            return None
        predicted_values = [
            float(prediction_row[0]) for prediction_row in prediction_rows
        ]
        actual_values = [float(prediction_row[1]) for prediction_row in prediction_rows]
        sample_count = len(predicted_values)
        residuals_kwh = [
            predicted_kwh - actual_kwh
            for predicted_kwh, actual_kwh in zip(predicted_values, actual_values)
        ]
        absolute_errors_kwh = [abs(residual_kwh) for residual_kwh in residuals_kwh]
        mae = sum(absolute_errors_kwh) / sample_count
        rmse = math.sqrt(
            sum((residual_kwh**2 for residual_kwh in residuals_kwh)) / sample_count
        )
        bias = sum(residuals_kwh) / sample_count
        coverage_10 = (
            sum(
                (
                    1
                    for predicted_kwh, actual_kwh in zip(
                        predicted_values, actual_values
                    )
                    if abs(predicted_kwh - actual_kwh) / max(actual_kwh, 0.01) <= 0.1
                )
            )
            / sample_count
        )
        coverage_20 = (
            sum(
                (
                    1
                    for predicted_kwh, actual_kwh in zip(
                        predicted_values, actual_values
                    )
                    if abs(predicted_kwh - actual_kwh) / max(actual_kwh, 0.01) <= 0.2
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
        event_count = 0
        end_date = date_str
        recent_start_date = (
            datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=30)
        ).strftime("%Y-%m-%d")
        baseline_start_date = (
            datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=90)
        ).strftime("%Y-%m-%d")
        for cloud_bucket, (cloud_minimum, cloud_maximum) in CLOUD_BUCKETS.items():
            for hour_bucket, bucket_hours in HOUR_BUCKETS.items():
                hour_placeholders = ",".join("?" * len(bucket_hours))
                for season_filter in (None, current_season):
                    try:
                        recent_bucket_mae = await self._bucket_mae(
                            scope,
                            recent_start_date,
                            end_date,
                            cloud_minimum,
                            cloud_maximum,
                            bucket_hours,
                            hour_placeholders,
                            season_filter,
                        )
                        baseline_bucket_mae = await self._bucket_mae(
                            scope,
                            baseline_start_date,
                            end_date,
                            cloud_minimum,
                            cloud_maximum,
                            bucket_hours,
                            hour_placeholders,
                            season_filter,
                        )
                        if (
                            recent_bucket_mae is None
                            or baseline_bucket_mae is None
                            or baseline_bucket_mae < 0.01
                        ):
                            continue
                        mae_ratio = recent_bucket_mae / baseline_bucket_mae
                        await self._db.save_drift_bucket_metric(
                            scope,
                            cloud_bucket,
                            hour_bucket,
                            season_filter,
                            {
                                "mae": recent_bucket_mae,
                                "mae_baseline": baseline_bucket_mae,
                                "mae_ratio": round(mae_ratio, 3),
                                "bias": 0.0,
                                "sample_count": 0,
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
                                f"Bucket {cloud_bucket}/{hour_bucket} MAE ratio {mae_ratio:.2f} (season={season_filter})",
                                "none",
                            )
                            event_count += 1
                    except Exception as error:
                        _LOGGER.debug(
                            "Bucket metric %s/%s/%s failed: %s",
                            cloud_bucket,
                            hour_bucket,
                            season_filter,
                            error,
                        )
                        continue
                continue
            continue
        return event_count

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
    ) -> Optional[float]:
        """Calculate MAE for a specific bucket."""
        if scope == "global":
            sql = f"\n                SELECT hp.prediction_kwh, hp.actual_kwh\n                FROM hourly_predictions hp\n                LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                    AND pw.weather_type = 'forecast'\n                WHERE hp.target_date >= ? AND hp.target_date <= ?\n                    AND hp.actual_kwh IS NOT NULL AND hp.actual_kwh > 0.01\n                    AND hp.prediction_kwh IS NOT NULL\n                    AND hp.target_hour IN ({hour_placeholders})\n                    AND COALESCE(pw.clouds, 50) >= ? AND COALESCE(pw.clouds, 50) < ?"
            query_parameters = [start_date, end_date] + hours + [cloud_min, cloud_max]
        else:
            sql = f"\n                SELECT ppg.prediction_kwh, ppg.actual_kwh\n                FROM prediction_panel_groups ppg\n                JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                    AND pw.weather_type = 'forecast'\n                WHERE hp.target_date >= ? AND hp.target_date <= ?\n                    AND ppg.actual_kwh IS NOT NULL AND ppg.actual_kwh > 0.01\n                    AND ppg.prediction_kwh IS NOT NULL\n                    AND ppg.group_name = ?\n                    AND hp.target_hour IN ({hour_placeholders})\n                    AND COALESCE(pw.clouds, 50) >= ? AND COALESCE(pw.clouds, 50) < ?"
            query_parameters = (
                [start_date, end_date, scope] + hours + [cloud_min, cloud_max]
            )
        if season:
            season_months = _get_season_months(season)
            season_placeholders = ",".join("?" * len(season_months))
            sql += f"\n                    AND CAST(strftime('%m', hp.target_date) AS INTEGER) IN ({season_placeholders})"
            query_parameters.extend(season_months)
        prediction_rows = await self._db.fetchall(sql, tuple(query_parameters))
        if not prediction_rows or len(prediction_rows) < 3:
            return None
        return sum(
            (
                abs(float(prediction_row[0]) - float(prediction_row[1]))
                for prediction_row in prediction_rows
            )
        ) / len(prediction_rows)

    async def _run_cusum_detection(self, date_str: str) -> int:
        """Run CUSUM detection for today's residuals. Returns event count."""
        event_count = 0
        cusum_threshold = self._config.get("cusum_threshold", 5.0)
        for scope_name in self._get_scopes():
            try:
                if scope_name == "global":
                    prediction_rows = await self._db.fetchall(
                        "SELECT prediction_kwh, actual_kwh\n                           FROM hourly_predictions\n                           WHERE target_date = ?\n                               AND actual_kwh IS NOT NULL AND actual_kwh > 0.01\n                               AND prediction_kwh IS NOT NULL",
                        (date_str,),
                    )
                else:
                    prediction_rows = await self._db.fetchall(
                        "SELECT ppg.prediction_kwh, ppg.actual_kwh\n                           FROM prediction_panel_groups ppg\n                           JOIN hourly_predictions hp ON ppg.prediction_id = hp.prediction_id\n                           WHERE hp.target_date = ? AND ppg.group_name = ?\n                               AND ppg.actual_kwh IS NOT NULL AND ppg.actual_kwh > 0.01\n                               AND ppg.prediction_kwh IS NOT NULL",
                        (date_str, scope_name),
                    )
                if not prediction_rows:
                    continue
                cusum_detector = self._cusum_detectors.get(scope_name, CUSUMDetector())
                for prediction_row in prediction_rows:
                    residual_kwh = float(prediction_row[0]) - float(prediction_row[1])
                    drift_direction = cusum_detector.update(
                        residual_kwh, threshold=cusum_threshold
                    )
                    if drift_direction:
                        severity = "critical"
                        cusum_value = max(
                            cusum_detector.cusum_pos, cusum_detector.cusum_neg
                        )
                        await self._create_drift_event(
                            date_str,
                            scope_name,
                            "cusum_alert",
                            severity,
                            None,
                            cusum_value,
                            cusum_threshold,
                            f"CUSUM alert: {drift_direction} (scope={scope_name}, value={cusum_value:.2f})",
                            "light_retrain",
                        )
                        cusum_detector.reset()
                        event_count += 1
                await self._db.save_drift_cusum_state(
                    scope_name, cusum_detector.to_dict()
                )
                self._cusum_detectors[scope_name] = cusum_detector
            except Exception as error:
                _LOGGER.debug("CUSUM for scope %s failed: %s", scope_name, error)
                continue
            continue
        return event_count

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

    async def _check_and_respond_to_drift(self, date_str: str) -> None:
        """Check rolling metrics for drift triggers and execute responses."""
        for scope_name in self._get_scopes():
            try:
                rolling_metrics = await self._db.get_drift_rolling_metrics(
                    scope_name, 30, None
                )
                if not rolling_metrics:
                    continue
                mae = rolling_metrics.get("mae", 0)
                bias = rolling_metrics.get("bias", 0)
                coverage_20 = rolling_metrics.get("coverage_20", 1.0)
                baseline_metrics = await self._db.get_drift_rolling_metrics(
                    scope_name, 60, None
                )
                baseline_mae = baseline_metrics.get("mae", 0) if baseline_metrics else 0
                if baseline_mae > 0.01:
                    mae_ratio = mae / baseline_mae
                    critical_ratio_threshold = self._config["mae_ratio_critical"]
                    warning_ratio_threshold = self._config["mae_ratio_warning"]
                    if mae_ratio > critical_ratio_threshold:
                        await self._create_drift_event(
                            date_str,
                            scope_name,
                            "mae_above_baseline",
                            "critical",
                            None,
                            mae_ratio,
                            critical_ratio_threshold,
                            f"MAE ratio {mae_ratio:.2f} exceeds critical threshold (scope={scope_name})",
                            "light_retrain",
                        )
                        await self._activate_physics_boost(scope_name)
                    elif mae_ratio > warning_ratio_threshold:
                        await self._create_drift_event(
                            date_str,
                            scope_name,
                            "mae_above_baseline",
                            "warning",
                            None,
                            mae_ratio,
                            warning_ratio_threshold,
                            f"MAE ratio {mae_ratio:.2f} exceeds warning threshold (scope={scope_name})",
                            "light_retrain",
                        )
                bias_threshold = self._config["bias_threshold"]
                if abs(bias) > bias_threshold:
                    await self._create_drift_event(
                        date_str,
                        scope_name,
                        "bias_shift",
                        "warning",
                        None,
                        abs(bias),
                        bias_threshold,
                        f"Systematic bias {bias:+.3f} kWh (scope={scope_name})",
                        "light_retrain",
                    )
                minimum_coverage_20 = self._config["coverage_20_min"]
                if coverage_20 < minimum_coverage_20:
                    await self._create_drift_event(
                        date_str,
                        scope_name,
                        "coverage_drop",
                        "warning",
                        None,
                        coverage_20,
                        minimum_coverage_20,
                        f"Coverage-20 dropped to {coverage_20:.0%} (scope={scope_name})",
                        "light_retrain",
                    )
                    continue
            except Exception as error:
                _LOGGER.debug(
                    "Drift response check for %s failed: %s", scope_name, error
                )

    async def _activate_physics_boost(self, scope: str) -> None:
        """Activate temporary physics boost for a scope."""
        boost_amount = self._config["physics_boost_amount"]
        max_boost_days = self._config["physics_boost_max_days"]
        self._physics_boost[scope] = {
            "boost": boost_amount,
            "activated_at": datetime.now().isoformat(),
            "max_days": max_boost_days,
        }
        _LOGGER.warning(
            "Physics boost activated: scope=%s, boost=%.0f%%, max_days=%d",
            scope,
            boost_amount * 100,
            max_boost_days,
        )

    def get_active_physics_boost(self, scope: str) -> float:
        """Get active physics boost for a scope. Returns 0.0 if no boost active.

        Used by forecast_rule_based_strategy._blend_hour() to reduce AI confidence.
        """
        boost_data = self._physics_boost.get(scope)
        if not boost_data:
            boost_data = self._physics_boost.get("global")
        if not boost_data:
            return 0.0
        try:
            activated_at = datetime.fromisoformat(boost_data["activated_at"])
            max_boost_days = boost_data.get("max_days", 7)
            if (datetime.now() - activated_at).days >= max_boost_days:
                self._physics_boost.pop(scope, None)
                return 0.0
            return boost_data.get("boost", 0.0)
        except (ValueError, KeyError):
            return 0.0

    async def get_status(self) -> Dict[str, Any]:
        """Get current drift status for the diagnostic sensor."""
        await self.initialize()
        try:
            seven_day_metrics = await self._db.get_drift_rolling_metrics(
                "global", 7, None
            )
            thirty_day_metrics = await self._db.get_drift_rolling_metrics(
                "global", 30, None
            )
            active_events = await self._db.get_active_drift_events()
            drift_status = "stable"
            if any(
                (
                    drift_event.get("severity") == "critical"
                    for drift_event in active_events
                )
            ):
                drift_status = "critical"
            elif any(
                (
                    drift_event.get("severity") == "warning"
                    for drift_event in active_events
                )
            ):
                drift_status = "warning"
            if drift_status == "stable" and active_events:
                drift_status = "recovering"
            per_group_status = {}
            for panel_group in self._panel_groups:
                group_name = (
                    panel_group.get("name", "unknown")
                    if isinstance(panel_group, dict)
                    else panel_group.name
                )
                group_metrics = await self._db.get_drift_rolling_metrics(
                    group_name, 30, None
                )
                group_status = "stable"
                if group_metrics:
                    group_events = [
                        record
                        for record in active_events
                        if record.get("scope") == group_name
                    ]
                    if any(
                        (
                            drift_event.get("severity") == "critical"
                            for drift_event in group_events
                        )
                    ):
                        group_status = "critical"
                    elif any(
                        (
                            drift_event.get("severity") == "warning"
                            for drift_event in group_events
                        )
                    ):
                        group_status = "warning"
                per_group_status[group_name] = {
                    "mae_30d": group_metrics.get("mae") if group_metrics else None,
                    "bias_30d": group_metrics.get("bias") if group_metrics else None,
                    "drift": group_status,
                }
                continue
            physics_boost_active = any(
                (
                    self.get_active_physics_boost(boost_scope) > 0
                    for boost_scope in self._get_scopes()
                )
            )
            return {
                "state": drift_status,
                "global_mae_7d": (
                    seven_day_metrics.get("mae") if seven_day_metrics else None
                ),
                "global_mae_30d": (
                    thirty_day_metrics.get("mae") if thirty_day_metrics else None
                ),
                "global_bias_30d": (
                    thirty_day_metrics.get("bias") if thirty_day_metrics else None
                ),
                "global_coverage_20_30d": (
                    thirty_day_metrics.get("coverage_20")
                    if thirty_day_metrics
                    else None
                ),
                "active_drift_events": len(active_events),
                "physics_boost_active": physics_boost_active,
                "per_group_status": per_group_status,
                "last_check": datetime.now().isoformat(),
            }
        except Exception as record:
            _LOGGER.error("Failed to get drift status: %s", record)
            return {"state": "unknown", "error": str(record)}


def _get_season_months(season: str) -> List[int]:
    """Get month numbers for a season."""
    months_by_season = {
        "winter": [12, 1, 2],
        "spring": [3, 4, 5],
        "summer": [6, 7, 8],
        "autumn": [9, 10, 11],
    }
    return months_by_season.get(season, [])
