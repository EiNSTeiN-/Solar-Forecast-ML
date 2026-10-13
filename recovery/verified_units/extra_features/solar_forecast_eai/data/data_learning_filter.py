# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Learning Filter for Solar Forecast Energy AI V16.2.0.
Central helper for excluding flagged hours from learning.
Uses database queries instead of JSON operations.

This module provides centralized logic for:
1. Checking if individual hours should be excluded from learning
2. Calculating the ratio of excluded hours for daily learning decisions
3. Filtering predictions for ML training

Design Decision:
- Individual hours with flags are excluded from hourly learning (ML, Panel Groups, etc.)
- Daily learning (Pattern Learner) is skipped if >25% of production hours are excluded
- This prevents learning from anomalous data (unexpected weather, sensor issues)

Note: "unexpected_clouds" is NOT excluded from Panel Group Efficiency learning.
The geometry_learner reclassifies weather (CLEAR->CLOUDY) based on DNI/DHI ratio.

@zara
"""
import logging
from typing import Any, Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from .data_io import DataManagerIO
from .db_manager import DatabaseManager

_LOGGER = logging.getLogger(__name__)
DAILY_LEARNING_EXCLUSION_THRESHOLD = 0.25


def should_exclude_hour_from_learning(
    prediction: Dict[str, Any], context: str = "general"
) -> Tuple[bool, str]:
    'Check if an hour should be excluded from learning. @zara\n\nChecks all relevant flags that indicate anomalous data:\n- exclude_from_learning (master flag)\n- has_weather_alert (unexpected weather event) - EXCEPT unexpected_clouds for panel_group\n- inverter_clipped (hardware limitation)\n- mppt_throttled (zero export + battery full)\n- snow_covered_panels (panels physically blocked)\n\nNOTE: frost_detected is NOT excluded! Frost (cold air) does not cover panels.\nPanels actually perform BETTER in cold temperatures due to higher efficiency.\nOnly snow/ice/rime that COVERS the panels should trigger exclusion via snow_covered_panels.\n\nArgs:\n    prediction: Hourly prediction dict from database\n    context: Learning context - "general" (default), "panel_group", or "ml"\n             "panel_group" context does NOT exclude unexpected_clouds since\n             the geometry_learner handles weather reclassification.\n\nReturns:\n    Tuple of (should_exclude: bool, reason: str)\n'
    flags = prediction.get("flags") or {}
    actual_weather = prediction.get("weather_actual") or {}
    weather_alert_type = flags.get("weather_alert_type") or ""
    allow_unexpected_clouds = (
        weather_alert_type == "unexpected_clouds" and context == "panel_group"
    )
    if allow_unexpected_clouds:
        _LOGGER.debug(
            "Allowing unexpected_clouds for panel_group learning (weather will be reclassified by geometry_learner)"
        )
    else:
        if flags.get("exclude_from_learning"):
            reason = weather_alert_type or "manually_excluded"
            return (True, reason)
        if flags.get("has_weather_alert"):
            return (True, weather_alert_type or "weather_alert")
    outlier_severity = flags.get("outlier_severity")
    if outlier_severity is not None and outlier_severity >= 0.8:
        return (True, "severe_outlier")
    if flags.get("is_outlier"):
        if outlier_severity is None or outlier_severity >= 0.8:
            return (True, "is_outlier")
    if flags.get("inverter_clipped"):
        return (True, "inverter_clipped")
    if flags.get("mppt_throttled"):
        mppt_reason = flags.get("mppt_throttle_reason") or "mppt_throttled"
        return (True, mppt_reason)
    if actual_weather.get("snow_covered_panels"):
        return (True, "snow_covered_panels")
    return (False, "")


def filter_predictions_for_learning(
    predictions: List[Dict[str, Any]],
    context: str = "general",
    log_exclusions: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    "Filter predictions list, removing hours that should be excluded. @zara\n\nArgs:\n    predictions: List of hourly predictions\n    context: Learning context for exclusion decisions\n    log_exclusions: Whether to log excluded hours\n\nReturns:\n    Tuple of (filtered_predictions, exclusion_stats)\n    exclusion_stats is a dict with counts per reason\n"
    filtered_predictions = []
    exclusion_counts = {}
    for prediction in predictions:
        should_exclude, reason = should_exclude_hour_from_learning(prediction, context)
        if should_exclude:
            exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
            if log_exclusions:
                sample_id = _get_sample_id(prediction)
                _LOGGER.debug("Excluding %s from learning: %s", sample_id, reason)
        else:
            filtered_predictions.append(prediction)
    if exclusion_counts and log_exclusions:
        excluded_hours = sum(exclusion_counts.values())
        _LOGGER.info(
            "Learning filter: Excluded %d hours (%d remaining). Reasons: %s",
            excluded_hours,
            len(filtered_predictions),
            exclusion_counts,
        )
    return (filtered_predictions, exclusion_counts)


def calculate_excluded_hours_ratio(
    predictions: List[Dict[str, Any]], production_hours_only: bool = True
) -> Tuple[float, int, int]:
    "Calculate the ratio of excluded hours to total production hours. @zara\n\nUsed for daily learning decisions (Pattern Learner, etc.)\n\nArgs:\n    predictions: List of hourly predictions for the day\n    production_hours_only: If True, only count hours with actual production\n\nReturns:\n    Tuple of (ratio, excluded_count, total_count)\n"
    if not predictions:
        return (0.0, 0, 0)
    if production_hours_only:
        production_predictions = [
            production_candidate
            for production_candidate in predictions
            if (production_candidate.get("actual_kwh") or 0) > 0
            or (production_candidate.get("prediction_kwh") or 0) > 0.01
        ]
    else:
        production_predictions = predictions
    if not production_predictions:
        return (0.0, 0, 0)
    excluded_count = 0
    for prediction in production_predictions:
        should_exclude, ignored_reason = should_exclude_hour_from_learning(prediction)
        if should_exclude:
            excluded_count += 1
    total_count = len(production_predictions)
    exclusion_ratio = excluded_count / total_count if total_count > 0 else 0.0
    return (exclusion_ratio, excluded_count, total_count)


def should_skip_daily_learning(
    predictions: List[Dict[str, Any]],
    threshold: float = DAILY_LEARNING_EXCLUSION_THRESHOLD,
) -> Tuple[bool, float, str]:
    "Check if daily learning should be skipped due to too many excluded hours. @zara\n\nArgs:\n    predictions: List of hourly predictions for the day\n    threshold: Maximum ratio of excluded hours (default: 0.25 = 25%)\n\nReturns:\n    Tuple of (should_skip: bool, ratio: float, reason: str)\n"
    exclusion_ratio, excluded_count, total_count = calculate_excluded_hours_ratio(
        predictions
    )
    if exclusion_ratio > threshold:
        reason = f"Too many excluded hours: {excluded_count}/{total_count} ({exclusion_ratio:.0%} > {threshold:.0%} threshold)"
        _LOGGER.info("Daily learning skipped: %s", reason)
        return (True, exclusion_ratio, reason)
    return (False, exclusion_ratio, "")


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
    "Per-group learning exclusion — only the affected group is excluded. @zara V17.0.0\n\nSystem-wide hardware flags still exclude ALL groups.\nSnow and shadow exclusion are per-group.\n\nArgs:\n    prediction: Hourly prediction dict\n    group_name: Panel group name to check\n    snow_per_group: Dict from weather_actual.snow_per_group\n    shadow_per_group: Dict mapping group_name → shadow detection result\n\nReturns:\n    Tuple of (should_exclude, reason)\n"
    flags = prediction.get("flags") or {}
    if flags.get("inverter_clipped"):
        return (True, "inverter_clipped")
    if flags.get("mppt_throttled"):
        return (True, flags.get("mppt_throttle_reason", "mppt_throttled"))
    if flags.get("has_weather_alert"):
        weather_alert_type = flags.get("weather_alert_type", "weather_alert")
        if weather_alert_type != "unexpected_clouds":
            return (True, weather_alert_type)
    if flags.get("is_outlier"):
        return (True, "is_outlier")
    if snow_per_group:
        group_snow = snow_per_group.get(group_name, {})
        if group_snow.get("snow_covered"):
            return (True, "snow_covered_panels")
    if shadow_per_group:
        group_shadow = shadow_per_group.get(group_name, {})
        if (
            group_shadow.get("shadow_type") == "heavy"
            and group_shadow.get("root_cause") == "building_tree_obstruction"
        ):
            return (True, "heavy_shadow_obstruction")
    return (False, "")


def should_skip_daily_learning_per_group(
    predictions: List[Dict[str, Any]],
    group_name: str,
    snow_per_group: Optional[Dict[str, Dict[str, Any]]] = None,
    shadow_per_group: Optional[Dict[str, Dict[str, Any]]] = None,
    threshold: float = DAILY_LEARNING_EXCLUSION_THRESHOLD,
) -> Tuple[bool, float, str]:
    "Per-group daily learning threshold check. @zara V17.0.0\n\nGroup A can learn even if Group B has 80% excluded hours.\n\nArgs:\n    predictions: List of hourly predictions for the day\n    group_name: Panel group to check\n    snow_per_group: Per-group snow states\n    shadow_per_group: Per-group shadow results\n    threshold: Maximum exclusion ratio (default 25%)\n\nReturns:\n    Tuple of (should_skip, ratio, reason)\n"
    if not predictions:
        return (False, 0.0, "")
    production_predictions = [
        production_candidate
        for production_candidate in predictions
        if (production_candidate.get("actual_kwh") or 0) > 0
        or (production_candidate.get("prediction_kwh") or 0) > 0.01
    ]
    if not production_predictions:
        return (False, 0.0, "")
    excluded_count = 0
    for prediction in production_predictions:
        group_excluded, ignored_reason = should_exclude_group_from_learning(
            prediction, group_name, snow_per_group, shadow_per_group
        )
        if group_excluded:
            excluded_count += 1
    total_count = len(production_predictions)
    exclusion_ratio = excluded_count / total_count if total_count > 0 else 0.0
    if exclusion_ratio > threshold:
        reason = f"Group {group_name}: {excluded_count}/{total_count} excluded ({exclusion_ratio:.0%} > {threshold:.0%})"
        _LOGGER.info("Per-group learning skipped: %s", reason)
        return (True, exclusion_ratio, reason)
    return (False, exclusion_ratio, "")


class LearningFilter(DataManagerIO):
    """Database-backed learning filter for advanced filtering operations. @zara

    Provides methods to query and filter predictions directly from the database
    for more efficient large-scale filtering operations.
    """

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        "Initialize learning filter. @zara\n\nArgs:\n    hass: Home Assistant instance\n    db_manager: DatabaseManager for DB operations\n"
        super().__init__(hass, db_manager)
        _LOGGER.debug("LearningFilter initialized with database backend")

    async def get_filtered_training_samples(
        self,
        days: int = 30,
        context: str = "general",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        "Get filtered training samples directly from database. @zara\n\nArgs:\n    days: Number of days to look back (if no dates specified)\n    context: Learning context for exclusion decisions\n    start_date: Optional start date filter\n    end_date: Optional end date filter\n\nReturns:\n    Tuple of (filtered_samples, exclusion_stats)\n"
        try:
            await self.ensure_initialized()
            if start_date and end_date:
                date_where = "hp.target_date BETWEEN ? AND ?"
                query_params = (start_date, end_date)
            else:
                from datetime import timedelta as timedelta_type

                cutoff_date = (
                    (dt_util.now() - timedelta_type(days=days)).date().isoformat()
                )
                date_where = "hp.target_date >= ?"
                query_params = (cutoff_date,)
            query = f"\n                SELECT\n                    hp.prediction_id,\n                    hp.target_date,\n                    hp.target_hour,\n                    hp.target_month,\n                    hp.target_season,\n                    hp.prediction_kwh,\n                    hp.actual_kwh,\n                    hp.accuracy_percent,\n                    hp.error_kwh,\n                    hp.is_production_hour,\n                    hp.is_peak_hour,\n                    hp.is_outlier,\n                    hp.has_weather_alert,\n                    hp.inverter_clipped,\n                    pw.temperature as weather_temperature,\n                    pw.solar_radiation_wm2 as weather_radiation,\n                    pw.clouds as weather_clouds,\n                    pa.sun_elevation_deg,\n                    pa.theoretical_max_kwh,\n                    hp.exclude_from_learning,\n                    hp.mppt_throttled,\n                    hp.mppt_throttle_reason,\n                    hp.weather_alert_type\n                FROM hourly_predictions hp\n                LEFT JOIN prediction_weather pw ON hp.prediction_id = pw.prediction_id\n                    AND pw.weather_type = 'forecast'\n                LEFT JOIN prediction_astronomy pa ON hp.prediction_id = pa.prediction_id\n                WHERE hp.actual_kwh IS NOT NULL\n                AND {date_where}\n                ORDER BY hp.target_datetime\n            "
            rows = await self.db.fetchall(query, query_params)
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
        "Get exclusion statistics for a specific day. @zara\n\nArgs:\n    date: Date string YYYY-MM-DD\n\nReturns:\n    Dict with exclusion statistics\n"
        try:
            await self.ensure_initialized()
            rows = await self.db.fetchall(
                "SELECT\n                    hp.prediction_id,\n                    hp.target_hour,\n                    hp.prediction_kwh,\n                    hp.actual_kwh,\n                    hp.is_production_hour,\n                    hp.has_weather_alert,\n                    hp.inverter_clipped,\n                    hp.exclude_from_learning,\n                    hp.mppt_throttled,\n                    hp.mppt_throttle_reason,\n                    hp.weather_alert_type,\n                    hwa.snow_covered_panels,\n                    hp.is_outlier\n                FROM hourly_predictions hp\n                LEFT JOIN hourly_weather_actual hwa\n                    ON hp.target_date = hwa.date AND hp.target_hour = hwa.hour\n                WHERE hp.target_date = ?",
                (date,),
            )
            if not rows:
                return (
                    (
                        {
                            "date": date,
                            "total_hours": 0,
                            "excluded_hours": 0,
                            "exclusion_ratio": 0.0,
                            "exclusion_reasons": {},
                            "skip_daily_learning": False,
                        }
                        if True
                        else None
                    )
                    if True
                    else None
                )
            predictions = []
            for row in rows:
                predictions.append(
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
                            "is_outlier": bool(row[12]) if row[12] else False,
                        },
                        "weather_actual": {
                            "snow_covered_panels": bool(row[11]) if row[11] else False
                        },
                    }
                )
                continue
            exclusion_ratio, excluded_count, total_count = (
                calculate_excluded_hours_ratio(predictions)
            )
            skip_daily_learning, unused_result, skip_reason = (
                should_skip_daily_learning(predictions)
            )
            unused_result, exclusion_counts = filter_predictions_for_learning(
                predictions, log_exclusions=False
            )
            return (
                (
                    {
                        "date": date,
                        "total_hours": total_count,
                        "excluded_hours": excluded_count,
                        "exclusion_ratio": round(exclusion_ratio, 3),
                        "exclusion_reasons": exclusion_counts,
                        "skip_daily_learning": skip_daily_learning,
                        "skip_reason": skip_reason if skip_daily_learning else None,
                    }
                    if True
                    else None
                )
                if True
                else None
            )
        except Exception as error:
            _LOGGER.error("Failed to get daily exclusion stats: %s", error)
            return ({} if True else None) if True else None

    async def mark_hour_excluded(self, date: str, hour: int, reason: str) -> bool:
        "Mark a specific hour as excluded from learning. @zara\n\nArgs:\n    date: Date string YYYY-MM-DD\n    hour: Hour (0-23)\n    reason: Exclusion reason\n\nReturns:\n    True if successful\n"
        try:
            await self.ensure_initialized()
            prediction_id = f"{date}_{hour:02d}"
            await self.db.execute(
                "UPDATE hourly_predictions\n                   SET has_weather_alert = TRUE\n                   WHERE prediction_id = ?",
                (prediction_id,),
            )
            _LOGGER.info(
                "Marked %s as excluded from learning: %s", prediction_id, reason
            )
        except Exception as error:
            _LOGGER.error("Failed to mark hour excluded: %s", error)
            return False
        return True

    async def get_learning_quality_report(self, days: int = 30) -> Dict[str, Any]:
        "Generate a quality report for learning data. @zara\n\nArgs:\n    days: Number of days to analyze\n\nReturns:\n    Dict with quality metrics\n"
        try:
            await self.ensure_initialized()
            from datetime import timedelta as timedelta_type

            cutoff_date = (dt_util.now() - timedelta_type(days=days)).date().isoformat()
            total_count_row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL AND target_date >= ?",
                (cutoff_date,),
            )
            total_samples = total_count_row[0] if total_count_row else 0
            weather_alert_count_row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL\n                   AND target_date >= ?\n                   AND has_weather_alert = TRUE",
                (cutoff_date,),
            )
            weather_alert_samples = (
                weather_alert_count_row[0] if weather_alert_count_row else 0
            )
            clipped_count_row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL\n                   AND target_date >= ?\n                   AND inverter_clipped = TRUE",
                (cutoff_date,),
            )
            clipped_samples = clipped_count_row[0] if clipped_count_row else 0
            throttled_count_row = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL\n                   AND target_date >= ?\n                   AND mppt_throttled = TRUE",
                (cutoff_date,),
            )
            throttled_samples = throttled_count_row[0] if throttled_count_row else 0
            daily_rows = await self.db.fetchall(
                "SELECT target_date, COUNT(*) as total,\n                          SUM(CASE WHEN has_weather_alert OR inverter_clipped\n                              OR mppt_throttled THEN 1 ELSE 0 END) as excluded\n                   FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL AND target_date >= ?\n                   GROUP BY target_date",
                (cutoff_date,),
            )
            high_exclusion_days = []
            for row in daily_rows:
                if row[1] > 0:
                    exclusion_ratio = row[2] / row[1]
                    if exclusion_ratio > DAILY_LEARNING_EXCLUSION_THRESHOLD:
                        high_exclusion_days.append(
                            {
                                "date": row[0],
                                "total": row[1],
                                "excluded": row[2],
                                "ratio": round(exclusion_ratio, 3),
                            }
                        )
            usable_samples = (
                total_samples
                - weather_alert_samples
                - clipped_samples
                - throttled_samples
            )
            return {
                "period_days": days,
                "total_samples": total_samples,
                "usable_samples": usable_samples,
                "excluded_samples": total_samples - usable_samples,
                "usable_ratio": (
                    round(usable_samples / total_samples, 3) if total_samples > 0 else 0
                ),
                "exclusion_breakdown": {
                    "weather_alerts": weather_alert_samples,
                    "inverter_clipped": clipped_samples,
                    "mppt_throttled": throttled_samples,
                },
                "high_exclusion_days": high_exclusion_days,
                "high_exclusion_days_count": len(high_exclusion_days),
            }
        except Exception as error:
            _LOGGER.error("Failed to generate learning quality report: %s", error)
            return {}
