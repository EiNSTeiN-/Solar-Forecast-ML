# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
Learning Filter for Solar Forecast ML V16.2.0.
Central helper for excluding flagged hours from learning.
Uses database queries instead of JSON operations.

This module provides centralized logic for:
1. Checking if individual hours should be excluded from learning
2. Calculating the ratio of excluded hours for daily learning decisions
3. Filtering predictions for ML training

Design Decision:
- Technical curtailment and invalid target data are excluded from learning.
- Stable weather forecast misses stay learnable because they are real PV outcomes
  for the forecast inputs the model actually saw.
- Short local weather events are operational triggers, not learning targets.
- Clean evaluation (customer forecast quality) excludes only technical
  limits and invalid measurements, not weather forecast misses.

@zara
"""
import logging
from typing import Any, Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from ..const import CURTAILMENT_THROTTLE_REASON_SQL, CURTAILMENT_THROTTLE_REASONS
from .data_io import DataManagerIO
from .db_manager import DatabaseManager

_LOGGER = logging.getLogger(__name__)
DAILY_LEARNING_EXCLUSION_THRESHOLD = 0.25


def throttle_reason_excludes_from_learning(reason: Optional[str]) -> bool:
    """Return True when an MPPT throttle reason is a curtailment learning leak."""
    return bool(reason) and reason in CURTAILMENT_THROTTLE_REASONS


HOURLY_LEARNING_ELIGIBILITY_SQL = f"\n    AND COALESCE(hp.exclude_from_learning, 0) = 0\n    AND COALESCE(hp.actual_backfill_blocked, 0) = 0\n    AND hp.data_quarantine_reason IS NULL\n    AND COALESCE(hp.mppt_throttled, 0) = 0\n    AND COALESCE(hp.inverter_clipped, 0) = 0\n    AND COALESCE(hp.mppt_throttle_reason, '') NOT IN ({CURTAILMENT_THROTTLE_REASON_SQL})\n"
PANEL_GROUP_LEARNING_ELIGIBILITY_SQL = f"\n    AND COALESCE(hp.exclude_from_learning, 0) = 0\n    AND COALESCE(hp.actual_backfill_blocked, 0) = 0\n    AND hp.data_quarantine_reason IS NULL\n    AND COALESCE(hp.mppt_throttled, 0) = 0\n    AND COALESCE(hp.inverter_clipped, 0) = 0\n    AND COALESCE(hp.mppt_throttle_reason, '') NOT IN ({CURTAILMENT_THROTTLE_REASON_SQL})\n    AND COALESCE(ppg.exclude_from_learning_group, 0) = 0\n    AND COALESCE(ppg.topology_compatible, 1) = 1\n"
CLEAN_EVALUATION_ELIGIBILITY_SQL = f"\n    {HOURLY_LEARNING_ELIGIBILITY_SQL}\n    AND COALESCE(hp.exclude_from_clean_evaluation, 0) = 0\n"
LEARNABLE_WEATHER_MISS_ALERT_TYPES = frozenset(
    {"unexpected_rain", "unexpected_snow", "unexpected_clouds"}
)
WEATHER_ALERT_UNEXPECTED_SUN = "unexpected_sun"
WEATHER_ALERT_SNOW_COVERED_PANELS = "snow_covered_panels"
QUALITY_EXCLUDED_WEATHER_ALERT_TYPES = frozenset({WEATHER_ALERT_SNOW_COVERED_PANELS})
PRODUCTION_MISS_MIN_RATIO = 1.5
PRODUCTION_MISS_MIN_DELTA_KWH = 0.15
PRODUCTION_MISS_MIN_PREDICTION_KWH = 0.08
PRODUCTION_MISS_UPLIFT_CAP = 1.35
WEATHER_MISS_CLASS_FORECAST_MISS = "forecast_miss"
WEATHER_MISS_CLASS_LEGACY_FORECAST_MISS = "legacy_forecast_miss"
WEATHER_MISS_CLASS_LOCAL_EVENT = "local_event"
WEATHER_MISS_CLASS_TECHNICAL = "technical"
WEATHER_MISS_CLASS_UNKNOWN = "unknown"
WEATHER_MISS_CLASS_NON_LEARNABLE_WEATHER = "non_learnable_weather"


def production_miss_uplift(
    actual_kwh: Optional[float], prediction_kwh: Optional[float]
) -> Optional[float]:
    """Return a capped rest-of-day uplift, or None when the hour is not a miss."""
    try:
        actual = float(actual_kwh)
        prediction = float(prediction_kwh)
    except (TypeError, ValueError):
        return
    if prediction < PRODUCTION_MISS_MIN_PREDICTION_KWH:
        return
    if actual - prediction < PRODUCTION_MISS_MIN_DELTA_KWH:
        return
    ratio = actual / prediction
    if ratio < PRODUCTION_MISS_MIN_RATIO:
        return
    return min(PRODUCTION_MISS_UPLIFT_CAP, ratio)


def is_learnable_weather_miss(
    alert_type: Optional[str], weather_miss_class: Optional[str] = None
) -> bool:
    """Return True for weather forecast misses that should remain learnable."""
    if not alert_type or alert_type not in LEARNABLE_WEATHER_MISS_ALERT_TYPES:
        return False
    return weather_miss_class in (
        WEATHER_MISS_CLASS_FORECAST_MISS,
        WEATHER_MISS_CLASS_LEGACY_FORECAST_MISS,
    )


def weather_alert_excludes_from_learning(
    alert_type: Optional[str], weather_miss_class: Optional[str] = None
) -> bool:
    """Return True when a weather alert makes the target unsuitable for learning."""
    if alert_type == WEATHER_ALERT_UNEXPECTED_SUN:
        return False
    return bool(alert_type) and (
        not is_learnable_weather_miss(alert_type, weather_miss_class)
    )


def weather_alert_excludes_from_clean_evaluation(alert_type: Optional[str]) -> bool:
    """Return True when a weather alert is not a forecast miss.

    Unexpected clouds/rain/snow/sun stay in customer forecast quality.
    Snow-covered panels are a physical outage, not a forecast error.
    """
    return bool(alert_type) and alert_type in QUALITY_EXCLUDED_WEATHER_ALERT_TYPES


def should_exclude_hour_from_learning(
    prediction: Dict[str, Any], context: str = "general"
) -> Tuple[bool, str]:
    """Check if an hour should be excluded from learning. @zara

    Checks all relevant flags that indicate non-learnable data:
    - exclude_from_learning (master flag)
    - has_weather_alert only when the alert describes non-learnable target data
    - inverter_clipped (hardware limitation)
    - mppt_throttled (zero export + battery full)
    - snow_covered_panels (panels physically blocked)

    NOTE: frost_detected is NOT excluded! Frost (cold air) does not cover panels.
    Panels actually perform BETTER in cold temperatures due to higher efficiency.
    Only snow/ice/rime that COVERS the panels should trigger exclusion via snow_covered_panels.

    Args:
        prediction: Hourly prediction dict from database
        context: Learning context - "general" (default), "panel_group", or "ml"
                 kept for API compatibility; weather forecast misses are now
                 learnable for all contexts.

    Returns:
        Tuple of (should_exclude: bool, reason: str)
    """
    flags = prediction.get("flags") or {}
    weather_actual = prediction.get("weather_actual") or {}
    weather_alert_type = flags.get("weather_alert_type") or ""
    weather_miss_class = flags.get("weather_miss_class")
    if flags.get("exclude_from_learning") or prediction.get("exclude_from_learning"):
        reason = weather_alert_type or "manually_excluded"
        return (True, reason)
    if flags.get("actual_backfill_blocked"):
        return (True, "actual_backfill_blocked")
    if flags.get("data_quarantine_reason"):
        return (True, f"data_quarantine:{flags['data_quarantine_reason']}")
    if flags.get("has_weather_alert") and weather_alert_excludes_from_learning(
        weather_alert_type, weather_miss_class
    ):
        return (True, weather_alert_type or "weather_alert")
    outlier_severity = flags.get("outlier_severity")
    if outlier_severity is not None and outlier_severity >= 0.8:
        return (True, "severe_outlier")
    if flags.get("is_outlier"):
        if outlier_severity is None or outlier_severity >= 0.8:
            return (True, "is_outlier")
    if flags.get("inverter_clipped"):
        return (True, "inverter_clipped")
    throttle_reason = (
        flags.get("mppt_throttle_reason")
        or prediction.get("mppt_throttle_reason")
        or ""
    )
    if flags.get("mppt_throttled") or prediction.get("mppt_throttled"):
        return (True, throttle_reason or "mppt_throttled")
    if throttle_reason in CURTAILMENT_THROTTLE_REASONS:
        return (True, throttle_reason)
    if weather_actual.get("snow_covered_panels"):
        return (True, "snow_covered_panels")
    return (False, "")


def hour_is_learnable(hour: Dict[str, Any], context: str = "general") -> bool:
    """Return True when the hour may enter learning, regime, or drift paths."""
    excluded, unused_reason = should_exclude_hour_from_learning(hour, context=context)
    return not excluded


def filter_predictions_for_learning(
    predictions: List[Dict[str, Any]],
    context: str = "general",
    log_exclusions: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Filter predictions list, removing hours that should be excluded. @zara

    Args:
        predictions: List of hourly predictions
        context: Learning context for exclusion decisions
        log_exclusions: Whether to log excluded hours

    Returns:
        Tuple of (filtered_predictions, exclusion_stats)
        exclusion_stats is a dict with counts per reason
    """
    filtered_predictions = []
    exclusion_counts = {}
    for hour_prediction in predictions:
        exclude_hour, reason = should_exclude_hour_from_learning(
            hour_prediction, context
        )
        if exclude_hour:
            exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
            if log_exclusions:
                sample_id = _get_sample_id(hour_prediction)
                _LOGGER.debug("Excluding %s from learning: %s", sample_id, reason)
        else:
            filtered_predictions.append(hour_prediction)
    if exclusion_counts and log_exclusions:
        excluded_total = sum(exclusion_counts.values())
        _LOGGER.info(
            "Learning filter: Excluded %d hours (%d remaining). Reasons: %s",
            excluded_total,
            len(filtered_predictions),
            exclusion_counts,
        )
    return (filtered_predictions, exclusion_counts)


def calculate_excluded_hours_ratio(
    predictions: List[Dict[str, Any]], production_hours_only: bool = True
) -> Tuple[float, int, int]:
    """Calculate the ratio of excluded hours to total production hours. @zara

    Used for daily learning decisions (Pattern Learner, etc.)

    Args:
        predictions: List of hourly predictions for the day
        production_hours_only: If True, only count hours with actual production

    Returns:
        Tuple of (ratio, excluded_count, total_count)
    """
    if not predictions:
        return (0.0, 0, 0)
    if production_hours_only:
        production_predictions = [
            prediction_row
            for prediction_row in predictions
            if (prediction_row.get("actual_kwh") or 0) > 0
            or (prediction_row.get("prediction_kwh") or 0) > 0.01
        ]
    else:
        production_predictions = predictions
    if not production_predictions:
        return (0.0, 0, 0)
    excluded_count = 0
    for hour_prediction in production_predictions:
        exclude_hour, unused_result = should_exclude_hour_from_learning(hour_prediction)
        if exclude_hour:
            excluded_count += 1
    total_count = len(production_predictions)
    ratio = excluded_count / total_count if total_count > 0 else 0.0
    return (ratio, excluded_count, total_count)


def should_skip_daily_learning(
    predictions: List[Dict[str, Any]],
    threshold: float = DAILY_LEARNING_EXCLUSION_THRESHOLD,
) -> Tuple[bool, float, str]:
    """Check if daily learning should be skipped due to too many excluded hours. @zara

    Args:
        predictions: List of hourly predictions for the day
        threshold: Maximum ratio of excluded hours (default: 0.25 = 25%)

    Returns:
        Tuple of (should_skip: bool, ratio: float, reason: str)
    """
    ratio, excluded_count, total_count = calculate_excluded_hours_ratio(predictions)
    if ratio > threshold:
        reason = f"Too many excluded hours: {excluded_count}/{total_count} ({ratio:.0%} > {threshold:.0%} threshold)"
        _LOGGER.info("Daily learning skipped: %s", reason)
        return (True, ratio, reason)
    return (False, ratio, "")


def _get_sample_id(prediction: Dict[str, Any]) -> str:
    """Get a human-readable sample ID for logging. @zara"""
    target_date = prediction.get("target_date", "????-??-??")
    target_hour = prediction.get("target_hour", "??")
    if isinstance(target_hour, int):
        return f"{target_date} {target_hour:02d}:00"
    return f"{target_date} {target_hour}:00"


def should_exclude_group_from_learning(
    prediction: Dict[str, Any],
    group_name: str,
    snow_per_group: Optional[Dict[str, Dict[str, Any]]] = None,
    shadow_per_group: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[bool, str]:
    """Per-group learning exclusion — only the affected group is excluded. @zara V17.0.0

    System-wide hardware flags still exclude ALL groups.
    Snow and shadow exclusion are per-group.

    Args:
        prediction: Hourly prediction dict
        group_name: Panel group name to check
        snow_per_group: Dict from weather_actual.snow_per_group
        shadow_per_group: Dict mapping group_name → shadow detection result

    Returns:
        Tuple of (should_exclude, reason)
    """
    flags = prediction.get("flags") or {}
    if flags.get("inverter_clipped"):
        return (True, "inverter_clipped")
    if flags.get("mppt_throttled"):
        return (True, flags.get("mppt_throttle_reason", "mppt_throttled"))
    if flags.get("has_weather_alert"):
        weather_alert_type = flags.get("weather_alert_type", "weather_alert")
        weather_miss_class = flags.get("weather_miss_class")
        if weather_alert_excludes_from_learning(weather_alert_type, weather_miss_class):
            return (True, weather_alert_type)
    if flags.get("is_outlier"):
        return (True, "is_outlier")
    if snow_per_group:
        snow_state = snow_per_group.get(group_name, {})
        if snow_state.get("snow_covered"):
            return (True, "snow_covered_panels")
    if shadow_per_group:
        shadow_state = shadow_per_group.get(group_name, {})
        shadow_type = shadow_state.get("shadow_type")
        root_cause = shadow_state.get("root_cause")
        if shadow_type != "not_evaluable":
            if (
                shadow_type in {"possible_obstruction", "building_tree_obstruction"}
                or root_cause in {"possible_obstruction", "building_tree_obstruction"}
                or shadow_state.get("forecast_learning_eligible") is False
            ):
                return (True, "possible_obstruction")
    return (False, "")


def should_skip_daily_learning_per_group(
    predictions: List[Dict[str, Any]],
    group_name: str,
    snow_per_group: Optional[Dict[str, Dict[str, Any]]] = None,
    shadow_per_group: Optional[Dict[str, Dict[str, Any]]] = None,
    threshold: float = DAILY_LEARNING_EXCLUSION_THRESHOLD,
) -> Tuple[bool, float, str]:
    """Per-group daily learning threshold check. @zara V17.0.0

    Group A can learn even if Group B has 80% excluded hours.

    Args:
        predictions: List of hourly predictions for the day
        group_name: Panel group to check
        snow_per_group: Per-group snow states
        shadow_per_group: Per-group shadow results
        threshold: Maximum exclusion ratio (default 25%)

    Returns:
        Tuple of (should_skip, ratio, reason)
    """
    if not predictions:
        return (False, 0.0, "")
    group_predictions = [
        prediction_row
        for prediction_row in predictions
        if (prediction_row.get("actual_kwh") or 0) > 0
        or (prediction_row.get("prediction_kwh") or 0) > 0.01
    ]
    if not group_predictions:
        return (False, 0.0, "")
    excluded_count = 0
    for hour_prediction in group_predictions:
        exclude_group, unused_result = should_exclude_group_from_learning(
            hour_prediction, group_name, snow_per_group, shadow_per_group
        )
        if exclude_group:
            excluded_count += 1
    total_count = len(group_predictions)
    ratio = excluded_count / total_count if total_count > 0 else 0.0
    if ratio > threshold:
        reason = f"Group {group_name}: {excluded_count}/{total_count} excluded ({ratio:.0%} > {threshold:.0%})"
        _LOGGER.info("Per-group learning skipped: %s", reason)
        return (True, ratio, reason)
    return (False, ratio, "")


class LearningFilter(DataManagerIO):
    """Database-backed learning filter for advanced filtering operations. @zara

    Provides methods to query and filter predictions directly from the database
    for more efficient large-scale filtering operations.
    """

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        """Initialize learning filter. @zara

        Args:
            hass: Home Assistant instance
            db_manager: DatabaseManager for DB operations
        """
        super().__init__(hass, db_manager)
        _LOGGER.debug("LearningFilter initialized with database backend")

    async def get_filtered_training_samples(
        self,
        days: int = 30,
        context: str = "general",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """Get filtered training samples directly from database. @zara

        Args:
            days: Number of days to look back (if no dates specified)
            context: Learning context for exclusion decisions
            start_date: Optional start date filter
            end_date: Optional end date filter

        Returns:
            Tuple of (filtered_samples, exclusion_stats)
        """
        try:
            await self.ensure_initialized()
            if start_date and end_date:
                date_condition = "hp.target_date BETWEEN ? AND ?"
                query_parameters = (start_date, end_date)
            else:
                from datetime import timedelta as timedelta

                cutoff_date = (dt_util.now() - timedelta(days=days)).date().isoformat()
                date_condition = "hp.target_date >= ?"
                query_parameters = (cutoff_date,)
            statement = f"\n                SELECT\n                    hp.prediction_id,\n                    hp.target_date,\n                    hp.target_hour,\n                    hp.target_month,\n                    hp.target_season,\n                    hp.prediction_kwh,\n                    hp.actual_kwh,\n                    hp.accuracy_percent,\n                    hp.error_kwh,\n                    hp.is_production_hour,\n                    hp.is_peak_hour,\n                    hp.is_outlier,\n                    hp.has_weather_alert,\n                    hp.inverter_clipped,\n                    pw.temperature as weather_temperature,\n                    pw.solar_radiation_wm2 as weather_radiation,\n                    pw.clouds as weather_clouds,\n                    pa.sun_elevation_deg,\n                    pa.theoretical_max_kwh,\n                    hp.exclude_from_learning,\n                    hp.mppt_throttled,\n                    hp.mppt_throttle_reason,\n                    hp.weather_alert_type,\n                    hp.weather_miss_class,\n                    hp.actual_backfill_blocked,\n                    hp.data_quarantine_reason\n                FROM hourly_predictions hp\n                LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                    AND pw.weather_type = 'forecast'\n                LEFT JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                WHERE hp.actual_kwh IS NOT NULL\n                AND {date_condition}\n                ORDER BY hp.target_datetime\n            "
            rows = await self.db.fetchall(statement, query_parameters)
            samples = []
            for row in rows:
                sample = {
                    "prediction_id": row[0],
                    "target_date": row[1],
                    "target_hour": row[2],
                    "target_month": row[3],
                    "target_season": row[4],
                    "prediction_kwh": row[5],
                    "actual_kwh": row[6],
                    "accuracy_percent": row[7],
                    "error_kwh": row[8],
                    "flags": {
                        "is_production_hour": bool(row[9]),
                        "is_peak_hour": bool(row[10]),
                        "is_outlier": bool(row[11]),
                        "has_weather_alert": bool(row[12]),
                        "inverter_clipped": bool(row[13]),
                        "exclude_from_learning": bool(row[19]) if row[19] else False,
                        "mppt_throttled": bool(row[20]) if row[20] else False,
                        "mppt_throttle_reason": row[21],
                        "weather_alert_type": row[22],
                        "weather_miss_class": row[23],
                        "actual_backfill_blocked": bool(row[24]),
                        "data_quarantine_reason": row[25],
                    },
                    "weather_forecast": {
                        "temperature": row[14],
                        "solar_radiation_wm2": row[15],
                        "clouds": row[16],
                    },
                    "astronomy": {
                        "sun_elevation_deg": row[17],
                        "theoretical_max_kwh": row[18],
                    },
                }
                samples.append(sample)
                continue
            return filter_predictions_for_learning(
                samples, context, log_exclusions=True
            )
        except Exception as error:
            _LOGGER.error("Failed to get filtered training samples: %s", error)
            return ([], {})

    async def get_daily_exclusion_stats(self, date: str) -> Dict[str, Any]:
        """Get exclusion statistics for a specific day. @zara

        Args:
            date: Date string YYYY-MM-DD

        Returns:
            Dict with exclusion statistics
        """
        try:
            await self.ensure_initialized()
            rows = await self.db.fetchall(
                "SELECT\n                    hp.prediction_id,\n                    hp.target_hour,\n                    hp.prediction_kwh,\n                    hp.actual_kwh,\n                    hp.is_production_hour,\n                    hp.has_weather_alert,\n                    hp.inverter_clipped,\n                    hp.exclude_from_learning,\n                    hp.mppt_throttled,\n                    hp.mppt_throttle_reason,\n                    hp.weather_alert_type,\n                    hp.weather_miss_class,\n                    hwa.snow_covered_panels,\n                    hp.is_outlier,\n                    hp.actual_backfill_blocked,\n                    hp.data_quarantine_reason\n                FROM hourly_predictions hp\n                LEFT JOIN hourly_weather_actual hwa\n                    ON hp.target_date = hwa.date AND hp.target_hour = hwa.hour\n                WHERE hp.target_date = ?",
                (date,),
            )
            if not rows:
                return {
                    "date": date,
                    "total_hours": 0,
                    "excluded_hours": 0,
                    "exclusion_ratio": 0.0,
                    "exclusion_reasons": {},
                    "skip_daily_learning": False,
                }
            hourly_predictions = []
            for row in rows:
                hourly_predictions.append(
                    {
                        "prediction_id": row[0],
                        "target_hour": row[1],
                        "prediction_kwh": row[2],
                        "actual_kwh": row[3],
                        "flags": {
                            "is_production_hour": bool(row[4]),
                            "has_weather_alert": bool(row[5]),
                            "inverter_clipped": bool(row[6]),
                            "exclude_from_learning": bool(row[7]) if row[7] else False,
                            "mppt_throttled": bool(row[8]) if row[8] else False,
                            "mppt_throttle_reason": row[9],
                            "weather_alert_type": row[10],
                            "weather_miss_class": row[11],
                            "is_outlier": bool(row[13]) if row[13] else False,
                            "actual_backfill_blocked": bool(row[14]),
                            "data_quarantine_reason": row[15],
                        },
                        "weather_actual": {
                            "snow_covered_panels": bool(row[12]) if row[12] else False
                        },
                    }
                )
                continue
            ratio, excluded_count, total_count = calculate_excluded_hours_ratio(
                hourly_predictions
            )
            skip_learning, unused_result, skip_reason = should_skip_daily_learning(
                hourly_predictions
            )
            unused_result, exclusion_counts = filter_predictions_for_learning(
                hourly_predictions, log_exclusions=False
            )
            return (
                (
                    {
                        "date": date,
                        "total_hours": total_count,
                        "excluded_hours": excluded_count,
                        "exclusion_ratio": round(ratio, 3),
                        "exclusion_reasons": exclusion_counts,
                        "skip_daily_learning": skip_learning,
                        "skip_reason": skip_reason if skip_learning else None,
                    }
                    if True
                    else None
                )
                if True
                else None
            )
        except Exception as error:
            _LOGGER.error("Failed to get daily exclusion stats: %s", error)
            return {}

    async def mark_hour_excluded(self, date: str, hour: int, reason: str) -> bool:
        """Mark a specific hour as excluded from learning. @zara

        Args:
            date: Date string YYYY-MM-DD
            hour: Hour (0-23)
            reason: Exclusion reason

        Returns:
            True if successful
        """
        try:
            await self.ensure_initialized()
            prediction_id = f"{date}_{hour:02d}"
            await self.db.execute(
                "UPDATE hourly_predictions\n                   SET exclude_from_learning = TRUE,\n                       exclude_from_clean_evaluation = TRUE,\n                       has_weather_alert = TRUE,\n                       weather_alert_type = ?\n                   WHERE prediction_id = ?",
                (reason, prediction_id),
            )
            _LOGGER.info(
                "Marked %s as excluded from learning: %s", prediction_id, reason
            )
        except Exception as error:
            _LOGGER.error("Failed to mark hour excluded: %s", error)
            return False
        return True

    async def get_learning_quality_report(self, days: int = 30) -> Dict[str, Any]:
        """Generate a quality report for learning data. @zara

        Args:
            days: Number of days to analyze

        Returns:
            Dict with quality metrics
        """
        try:
            await self.ensure_initialized()
            from datetime import timedelta as timedelta

            cutoff_date = (dt_util.now() - timedelta(days=days)).date().isoformat()
            total_row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL AND target_date >= ?",
                (cutoff_date,),
            )
            total_samples = total_row[0] if total_row else 0
            weather_alert_row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL\n                   AND target_date >= ?\n                   AND has_weather_alert = TRUE",
                (cutoff_date,),
            )
            weather_alert_count = weather_alert_row[0] if weather_alert_row else 0
            clipped_row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL\n                   AND target_date >= ?\n                   AND inverter_clipped = TRUE",
                (cutoff_date,),
            )
            clipped_count = clipped_row[0] if clipped_row else 0
            throttled_row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL\n                   AND target_date >= ?\n                   AND mppt_throttled = TRUE",
                (cutoff_date,),
            )
            throttled_count = throttled_row[0] if throttled_row else 0
            daily_rows = await self.db.fetchall(
                "SELECT target_date, COUNT(*) as total,\n                          SUM(CASE WHEN has_weather_alert OR inverter_clipped\n                              OR mppt_throttled THEN 1 ELSE 0 END) as excluded\n                   FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL AND target_date >= ?\n                   GROUP BY target_date",
                (cutoff_date,),
            )
            low_quality_days = []
            for row in daily_rows:
                if row[1] > 0:
                    ratio = row[2] / row[1]
                    if ratio > DAILY_LEARNING_EXCLUSION_THRESHOLD:
                        low_quality_days.append(
                            {
                                "date": row[0],
                                "total": row[1],
                                "excluded": row[2],
                                "ratio": round(ratio, 3),
                            }
                        )
            learnable_samples = (
                total_samples - weather_alert_count - clipped_count - throttled_count
            )
            return {
                "period_days": days,
                "total_samples": total_samples,
                "usable_samples": learnable_samples,
                "excluded_samples": total_samples - learnable_samples,
                "usable_ratio": (
                    round(learnable_samples / total_samples, 3)
                    if total_samples > 0
                    else 0
                ),
                "exclusion_breakdown": {
                    "weather_alerts": weather_alert_count,
                    "inverter_clipped": clipped_count,
                    "mppt_throttled": throttled_count,
                },
                "high_exclusion_days": low_quality_days,
                "high_exclusion_days_count": len(low_quality_days),
            }
        except Exception as error:
            _LOGGER.error("Failed to generate learning quality report: %s", error)
            return {}
