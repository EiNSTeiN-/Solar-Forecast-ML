# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Shadow fingerprint, analytics, and movement views."""
from __future__ import annotations
import logging
from datetime import timedelta
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from . import context
from .common import (
    _get_db,
    _panel_group_exclusion_reason,
    _table_columns,
    _time_context,
    local_only,
)
from ..core.dev_mode import is_dev_active, json_dev_required
from ..core.shadow_context import build_shadow_fingerprint, is_local_shadow_observation
from ..utils.time_utils import ha_today

_LOGGER = logging.getLogger(__name__)


class ShadowFingerprintView(HomeAssistantView):
    """Per-hour shadow detection fingerprint from SFML's shadow learning. @zara"""

    url = "/api/sfml_stats/solar/shadow_fingerprint"
    name = "api:sfml_stats:solar:shadow_fingerprint"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return hour-of-day + month-of-year shadow pattern data. @zara"""
        if not is_dev_active(context.HASS):
            return json_dev_required()
        try:
            if context.HASS is None:
                return web.json_response({"error": "not configured"}, status=404)
            group_name = request.query.get("group", "_system_")
            hourly_patterns = []
            seasonal_patterns = []
            available_groups = []
            history_since = None
            model_version = "unknown"
            try:
                async with _get_db() as connection:
                    try:
                        async with connection.execute(
                            "SELECT version FROM shadow_pattern_config WHERE id = 1"
                        ) as cursor:
                            row = await cursor.fetchone()
                            if row:
                                model_version = str(row[0] or "unknown")
                    except Exception as lookup_error:
                        _LOGGER.debug(
                            "shadow_pattern_config unavailable: %s", lookup_error
                        )
                    try:
                        async with connection.execute(
                            "SELECT MIN(date) FROM shadow_learning_history WHERE group_name = ?",
                            (group_name,),
                        ) as cursor:
                            row = await cursor.fetchone()
                            if row and row[0]:
                                history_since = str(row[0])
                    except Exception as lookup_error:
                        _LOGGER.debug(
                            "shadow_learning_history unavailable: %s", lookup_error
                        )
                    try:
                        async with connection.execute(
                            "SELECT DISTINCT group_name FROM shadow_pattern_hourly UNION SELECT DISTINCT group_name FROM shadow_pattern_seasonal"
                        ) as cursor:
                            for pattern_row in await cursor.fetchall():
                                if pattern_row[0]:
                                    available_groups.append(pattern_row[0])
                    except Exception:
                        pass
                    try:
                        async with connection.execute(
                            "SELECT hour, shadow_occurrence_rate, avg_shadow_percent,\n                                      pattern_type, confidence, sample_count, first_learned,\n                                      pct_building_tree, pct_weather_clouds, pct_low_sun, pct_other\n                               FROM shadow_pattern_hourly\n                               WHERE group_name = ?\n                               ORDER BY hour",
                            (group_name,),
                        ) as cursor:
                            for pattern_row in await cursor.fetchall():
                                hourly_patterns.append(
                                    {
                                        "hour": pattern_row[0],
                                        "shadow_occurrence_rate": pattern_row[1],
                                        "avg_shadow_percent": pattern_row[2],
                                        "pattern_type": pattern_row[3],
                                        "confidence": pattern_row[4],
                                        "sample_count": pattern_row[5],
                                        "first_learned": pattern_row[6],
                                        "pct_building_tree": pattern_row[7],
                                        "pct_weather_clouds": pattern_row[8],
                                        "pct_low_sun": pattern_row[9],
                                        "pct_other": pattern_row[10],
                                    }
                                )
                    except Exception as lookup_error:
                        _LOGGER.debug(
                            "shadow_pattern_hourly unavailable: %s", lookup_error
                        )
                    try:
                        async with connection.execute(
                            "SELECT month, hour, shadow_occurrence_rate, avg_shadow_percent,\n                                      dominant_cause, sample_count, confidence\n                               FROM shadow_pattern_seasonal\n                               WHERE group_name = ?\n                               ORDER BY month, hour",
                            (group_name,),
                        ) as cursor:
                            for pattern_row in await cursor.fetchall():
                                seasonal_patterns.append(
                                    {
                                        "month": pattern_row[0],
                                        "hour": pattern_row[1],
                                        "shadow_occurrence_rate": pattern_row[2],
                                        "avg_shadow_percent": pattern_row[3],
                                        "dominant_cause": pattern_row[4],
                                        "sample_count": pattern_row[5],
                                        "confidence": pattern_row[6],
                                    }
                                )
                    except Exception as lookup_error:
                        _LOGGER.debug(
                            "shadow_pattern_seasonal unavailable: %s", lookup_error
                        )
            except Exception as lookup_error:
                _LOGGER.debug("Shadow fingerprint lookup failed: %s", lookup_error)
            fingerprint = build_shadow_fingerprint(
                hourly_patterns if model_version == "2.0" else [],
                seasonal_patterns if model_version == "2.0" else [],
                history_since=history_since,
            )
            return web.json_response(
                {
                    "success": True,
                    "group": group_name,
                    "available_groups": available_groups,
                    "model_version": model_version,
                    "model_ready": model_version == "2.0",
                    **fingerprint,
                    "time_context": _time_context(),
                }
            )
        except Exception as request_error:
            _LOGGER.error(
                "Error in shadow fingerprint: %s", request_error, exc_info=True
            )
            return web.json_response({"error": str(request_error)}, status=500)


class ShadowAnalyticsView(HomeAssistantView):
    """Get shadow analytics data from DB. @zara"""

    url = "/api/sfml_stats/shadow_analytics"
    name = "api:sfml_stats:shadow_analytics"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return shadow analytics data as JSON. @zara"""
        try:
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "HASS not initialized"}, status=500
                )
            if not is_dev_active(context.HASS):
                return await self._public_shadow_summary()
            days = int(request.query.get("days", "30"))
            days = min(max(days, 7), 365)
            start_iso = (ha_today(context.HASS) - timedelta(days=days)).isoformat()
            async with _get_db() as connection:
                heatmap = {}
                async with connection.execute(
                    "SELECT p.target_date, p.target_hour,\n                              h.shadow_percent, h.shadow_type, h.root_cause,\n                              h.efficiency_ratio, h.loss_kwh, h.confidence\n                       FROM hourly_shadow_detection h\n                       JOIN hourly_predictions p ON h.prediction_id = p.prediction_id\n                       WHERE p.target_date >= ?\n                         AND p.target_hour >= 7 AND p.target_hour <= 18\n                       ORDER BY p.target_date, p.target_hour",
                    (start_iso,),
                ) as cursor:
                    for row in await cursor.fetchall():
                        date_key = row["target_date"]
                        hour = row["target_hour"]
                        if date_key not in heatmap:
                            heatmap[date_key] = {}
                        heatmap[date_key][hour] = {
                            "pct": round(row["shadow_percent"] or 0, 1),
                            "type": row["shadow_type"],
                            "cause": row["root_cause"],
                            "eff": round(row["efficiency_ratio"] or 0, 2),
                            "loss": round(row["loss_kwh"] or 0, 3),
                        }
                cause_counts = {}
                async with connection.execute(
                    "SELECT h.root_cause, COUNT(*) as cnt\n                       FROM hourly_shadow_detection h\n                       JOIN hourly_predictions p ON h.prediction_id = p.prediction_id\n                       WHERE p.target_date >= ?\n                         AND p.target_hour >= 7 AND p.target_hour <= 18\n                         AND h.shadow_type NOT IN ('none', 'not_evaluable', 'night')\n                         AND h.root_cause IN (\n                             'building_tree_obstruction', 'possible_obstruction'\n                         )\n                         AND COALESCE(p.mppt_throttled, 0) = 0\n                         AND COALESCE(p.inverter_clipped, 0) = 0\n                         AND COALESCE(p.exclude_from_clean_evaluation, 0) = 0\n                       GROUP BY h.root_cause\n                       ORDER BY cnt DESC",
                    (start_iso,),
                ) as cursor:
                    for row in await cursor.fetchall():
                        cause = row["root_cause"] or "unknown"
                        if cause != "night":
                            cause_counts[cause] = row["cnt"]
                daily_losses = []
                async with connection.execute(
                    "SELECT date, shadow_hours_count, cumulative_loss_kwh\n                       FROM daily_summary_shadow_analysis\n                       WHERE date >= ?\n                       ORDER BY date",
                    (start_iso,),
                ) as cursor:
                    for row in await cursor.fetchall():
                        daily_losses.append(
                            {
                                "date": row["date"],
                                "hours": row["shadow_hours_count"],
                                "loss_kwh": round(row["cumulative_loss_kwh"] or 0, 3),
                            }
                        )
                total_loss_kwh = sum(
                    (daily_loss["loss_kwh"] for daily_loss in daily_losses)
                )
                shadow_hours = sum((daily_loss["hours"] for daily_loss in daily_losses))
                days_with_shadow = sum(
                    (1 for daily_loss in daily_losses if daily_loss["hours"] > 0)
                )
                average_efficiency = None
                async with connection.execute(
                    "SELECT AVG(h.efficiency_ratio) as avg_eff\n                       FROM hourly_shadow_detection h\n                       JOIN hourly_predictions p ON h.prediction_id = p.prediction_id\n                       WHERE p.target_date >= ?\n                         AND p.target_hour >= 7 AND p.target_hour <= 18\n                         AND h.shadow_type NOT IN ('none', 'not_evaluable', 'night')\n                         AND h.root_cause IN (\n                             'building_tree_obstruction', 'possible_obstruction'\n                         )\n                         AND COALESCE(p.mppt_throttled, 0) = 0\n                         AND COALESCE(p.inverter_clipped, 0) = 0\n                         AND COALESCE(p.exclude_from_clean_evaluation, 0) = 0",
                    (start_iso,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row["avg_eff"] is not None:
                        average_efficiency = round(row["avg_eff"], 3)
                dominant_cause = (
                    max(cause_counts, key=cause_counts.get) if cause_counts else None
                )
                hourly_patterns = {}
                async with connection.execute(
                    "SELECT p.target_hour,\n                              AVG(h.shadow_percent) as avg_pct,\n                              COUNT(*) as total,\n                              SUM(CASE WHEN h.shadow_type <> 'none' THEN 1 ELSE 0 END) as shadow_cnt\n                       FROM hourly_shadow_detection h\n                       JOIN hourly_predictions p ON h.prediction_id = p.prediction_id\n                       WHERE p.target_date >= ?\n                         AND p.target_hour >= 7 AND p.target_hour <= 18\n                         AND h.shadow_type NOT IN ('not_evaluable', 'night')\n                         AND (\n                             h.shadow_type = 'none'\n                             OR h.root_cause IN (\n                                 'building_tree_obstruction', 'possible_obstruction'\n                             )\n                         )\n                         AND COALESCE(p.mppt_throttled, 0) = 0\n                         AND COALESCE(p.inverter_clipped, 0) = 0\n                         AND COALESCE(p.exclude_from_clean_evaluation, 0) = 0\n                       GROUP BY p.target_hour\n                       ORDER BY p.target_hour",
                    (start_iso,),
                ) as cursor:
                    for row in await cursor.fetchall():
                        hour = row["target_hour"]
                        hourly_patterns[hour] = {
                            "avg_pct": round(row["avg_pct"] or 0, 1),
                            "occurrence_rate": (
                                round(row["shadow_cnt"] / row["total"] * 100, 1)
                                if row["total"] > 0
                                else 0
                            ),
                        }
                learning_stats = {}
                async with connection.execute(
                    "SELECT total_days_learned, total_hours_learned,\n                              last_learning_date, patterns_detected\n                         FROM shadow_pattern_config WHERE id = 1"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        learning_stats = {
                            "days_learned": row["total_days_learned"],
                            "hours_learned": row["total_hours_learned"],
                            "last_date": row["last_learning_date"],
                            "patterns_detected": row["patterns_detected"],
                        }
            return web.json_response(
                {
                    "success": True,
                    "data": {
                        "stats": {
                            "total_loss_kwh": round(total_loss_kwh, 2),
                            "shadow_hours": shadow_hours,
                            "days_with_shadow": days_with_shadow,
                            "days_analyzed": len(daily_losses),
                            "avg_efficiency": average_efficiency,
                            "dominant_cause": dominant_cause,
                        },
                        "heatmap": heatmap,
                        "causes": cause_counts,
                        "daily_loss": daily_losses,
                        "hourly_pattern": hourly_patterns,
                        "learning": learning_stats,
                    },
                }
            )
        except Exception as request_error:
            _LOGGER.error("Error getting shadow analytics: %s", request_error)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )

    async def _public_shadow_summary(self) -> web.Response:
        """Today's shading loss only. Missing tables stay silent."""
        today_loss_kwh = None
        try:
            today_iso = ha_today(context.HASS).isoformat()
            async with _get_db() as connection:
                async with connection.execute(
                    "SELECT cumulative_loss_kwh FROM daily_summary_shadow_analysis WHERE date = ?",
                    (today_iso,),
                ) as cursor:
                    row = await cursor.fetchone()
            if row is not None and row["cumulative_loss_kwh"] is not None:
                today_loss_kwh = round(float(row["cumulative_loss_kwh"]), 1)
        except Exception:
            today_loss_kwh = None
        return web.json_response(
            {
                "success": True,
                "data": {"public_summary": {"today_loss_kwh": today_loss_kwh}},
            }
        )


class ShadowMovementView(HomeAssistantView):
    """Per-group shadow movement data for animation. @zara"""

    url = "/api/sfml_stats/solar/shadow_movement"
    name = "api:sfml_stats:solar:shadow_movement"
    requires_auth = False

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return per-group hourly shadow data. Supports single date or days range. @zara"""
        if not is_dev_active(context.HASS):
            return json_dev_required()
        try:
            if context.HASS is None:
                return web.json_response(
                    {"success": False, "error": "HASS not initialized"}, status=500
                )
            requested_days = request.query.get("days")
            requested_date = request.query.get("date")
            if requested_days:
                days = min(max(int(requested_days), 1), 30)
                today = ha_today(context.HASS)
                start_date = today - timedelta(days=days - 1)
                requested_dates = [
                    (start_date + timedelta(days=day_offset)).isoformat()
                    for day_offset in range(days)
                ]
            else:
                requested_dates = [requested_date or ha_today(context.HASS).isoformat()]
            movement_by_date = {}
            group_names = set()
            async with _get_db() as connection:
                panel_group_columns = await _table_columns(
                    connection, "prediction_panel_groups"
                )
                hourly_prediction_columns = await _table_columns(
                    connection, "hourly_predictions"
                )

                def hp_expr(name: str, default: str) -> str:
                    return (
                        f"hp.{name}"
                        if name in hourly_prediction_columns
                        else f"{default} AS {name}"
                    )

                def ppg_expr(name: str, default: str) -> str:
                    return (
                        f"ppg.{name}"
                        if name in panel_group_columns
                        else f"{default} AS {name}"
                    )

                for movement_date in requested_dates:
                    hour_data = {}
                    async with connection.execute(
                        f"SELECT ppg.prediction_id, ppg.group_name,\n                                  ppg.prediction_kwh, ppg.actual_kwh,\n                                  hp.target_hour,\n                                  {hp_expr('exclude_from_learning', '0')},\n                                  {hp_expr('exclude_from_clean_evaluation', '0')},\n                                  {hp_expr('mppt_throttled', '0')},\n                                  {hp_expr('inverter_clipped', '0')},\n                                  {hp_expr('mppt_throttle_reason', 'NULL')},\n                                  {hp_expr('has_weather_alert', '0')},\n                                  {hp_expr('weather_alert_type', 'NULL')},\n                                  {ppg_expr('exclude_from_learning_group', '0')},\n                                  {ppg_expr('exclusion_reason_group', 'NULL')},\n                                  {ppg_expr('snow_covered_group', '0')},\n                                  {ppg_expr('topology_compatible', '1')},\n                                  {ppg_expr('topology_exclusion_reason', 'NULL')}\n                           FROM prediction_panel_groups ppg\n                           JOIN hourly_predictions hp ON hp.prediction_id = ppg.prediction_id\n                           WHERE hp.target_date = ?\n                           ORDER BY hp.target_hour, ppg.group_name",
                        (movement_date,),
                    ) as cursor:
                        rows = await cursor.fetchall()
                        for row in rows:
                            hour = row["target_hour"]
                            if hour < 6 or hour > 20:
                                continue
                            if hour not in hour_data:
                                hour_data[hour] = {"groups": {}}
                            group_name = row["group_name"]
                            exclusion_reason = _panel_group_exclusion_reason(row)
                            hour_data[hour]["groups"][group_name] = {
                                "prediction": round(row["prediction_kwh"] or 0, 4),
                                "actual": (
                                    None
                                    if row["actual_kwh"] is None
                                    else round(row["actual_kwh"], 4)
                                ),
                                "excluded": exclusion_reason is not None,
                                "exclusion_reason": exclusion_reason,
                            }
                    async with connection.execute(
                        "SELECT acpg.hour, acpg.group_name,\n                                  acpg.theoretical_kwh, acpg.poa_wm2\n                           FROM astronomy_cache_panel_groups acpg\n                           WHERE acpg.cache_date = ?\n                             AND acpg.hour BETWEEN 6 AND 20\n                           ORDER BY acpg.hour, acpg.group_name",
                        (movement_date,),
                    ) as cursor:
                        rows = await cursor.fetchall()
                        for row in rows:
                            hour = row["hour"]
                            if hour not in hour_data:
                                hour_data[hour] = {"groups": {}}
                            group_name = row["group_name"]
                            if group_name not in hour_data[hour]["groups"]:
                                hour_data[hour]["groups"][group_name] = {}
                            hour_data[hour]["groups"][group_name]["theoretical"] = (
                                round(row["theoretical_kwh"] or 0, 4)
                            )
                            hour_data[hour]["groups"][group_name]["poa"] = round(
                                row["poa_wm2"] or 0, 1
                            )
                    async with connection.execute(
                        "SELECT p.target_hour,\n                                  h.shadow_percent, h.shadow_type, h.root_cause,\n                                  h.efficiency_ratio, h.loss_kwh, h.confidence\n                           FROM hourly_shadow_detection h\n                           JOIN hourly_predictions p ON h.prediction_id = p.prediction_id\n                           WHERE p.target_date = ?\n                             AND p.target_hour BETWEEN 6 AND 20\n                           ORDER BY p.target_hour",
                        (movement_date,),
                    ) as cursor:
                        rows = await cursor.fetchall()
                        for row in rows:
                            hour = row["target_hour"]
                            if hour not in hour_data:
                                hour_data[hour] = {"groups": {}}
                            hour_data[hour]["shadow"] = {
                                "pct": round(row["shadow_percent"] or 0, 1),
                                "type": row["shadow_type"] or "none",
                                "cause": row["root_cause"] or "unknown",
                                "confidence": round(row["confidence"] or 0, 2),
                                "local_shadow_confirmed": is_local_shadow_observation(
                                    row["root_cause"], row["confidence"]
                                ),
                                "efficiency": round(
                                    (row["efficiency_ratio"] or 1) * 100, 1
                                ),
                                "loss": round(row["loss_kwh"] or 0, 3),
                            }
                    throttle_reasons = {
                        "curtailment",
                        "battery_full",
                        "manual_limit",
                        "external_limit",
                        "inverter_limit",
                        "mppt_throttled",
                        "inverter_clipped",
                        "demand_limited_zero_export",
                        "suspected_battery_curtailment",
                    }
                    for hour, hour_entry in hour_data.items():
                        valid_actual_kwh = 0.0
                        valid_theoretical_kwh = 0.0
                        valid_group_count = 0
                        excluded_group_count = 0
                        throttled_group_count = 0
                        exclusion_counts = {}
                        for group_name, group_data in hour_entry.get(
                            "groups", {}
                        ).items():
                            theoretical_kwh = group_data.get("theoretical", 0)
                            actual_kwh = group_data.get("actual")
                            if group_data.get("excluded"):
                                group_data["efficiency"] = None
                                group_data["shadow_pct"] = None
                                excluded_group_count += 1
                                group_exclusion_reason = (
                                    group_data.get("exclusion_reason") or "excluded"
                                )
                                exclusion_counts[group_exclusion_reason] = (
                                    exclusion_counts.get(group_exclusion_reason, 0) + 1
                                )
                                if group_exclusion_reason in throttle_reasons:
                                    throttled_group_count += 1
                                    continue
                                continue
                            if actual_kwh is not None:
                                if theoretical_kwh > 0.001:
                                    group_data["efficiency"] = round(
                                        min(actual_kwh / theoretical_kwh * 100, 100), 1
                                    )
                                    group_data["shadow_pct"] = round(
                                        max(
                                            0,
                                            min(
                                                (1 - actual_kwh / theoretical_kwh)
                                                * 100,
                                                100,
                                            ),
                                        ),
                                        1,
                                    )
                                    valid_actual_kwh += max(float(actual_kwh), 0.0)
                                    valid_theoretical_kwh += max(
                                        float(theoretical_kwh), 0.0
                                    )
                                    valid_group_count += 1
                                    continue
                            if actual_kwh is None:
                                group_data["efficiency"] = None
                                group_data["shadow_pct"] = None
                                continue
                            group_data["efficiency"] = 0
                            group_data["shadow_pct"] = 0
                            continue
                        if valid_theoretical_kwh > 0.001 and valid_group_count > 0:
                            hour_efficiency_percent = min(
                                valid_actual_kwh / valid_theoretical_kwh * 100, 100
                            )
                            hour_shadow_percent = max(
                                0, min(100, 100 - hour_efficiency_percent)
                            )
                            shadow_detection = hour_entry.get("shadow") or {}
                            hour_entry["clean_shadow"] = {
                                "pct": round(hour_shadow_percent, 1),
                                "efficiency": round(hour_efficiency_percent, 1),
                                "loss": round(
                                    max(valid_theoretical_kwh - valid_actual_kwh, 0), 3
                                ),
                                "cause": shadow_detection.get("cause") or "unknown",
                                "local_shadow_confirmed": bool(
                                    shadow_detection.get("local_shadow_confirmed")
                                ),
                                "valid_groups": valid_group_count,
                                "excluded_groups": excluded_group_count,
                                "throttled_groups": throttled_group_count,
                                "exclusion_reasons": exclusion_counts,
                            }
                            continue
                        hour_entry["clean_shadow"] = {
                            "pct": 0,
                            "efficiency": None,
                            "loss": 0,
                            "cause": (
                                "mppt_throttled"
                                if throttled_group_count
                                else "excluded" if excluded_group_count else "unknown"
                            ),
                            "valid_groups": 0,
                            "excluded_groups": excluded_group_count,
                            "throttled_groups": throttled_group_count,
                            "exclusion_reasons": exclusion_counts,
                        }
                        continue
                    for hour_entry in hour_data.values():
                        group_names.update(hour_entry.get("groups", {}).keys())
                    has_positive_actuals = any(
                        (
                            (group_data.get("actual") or 0) > 0
                            for hour_entry in hour_data.values()
                            for group_data in hour_entry.get("groups", {}).values()
                        )
                    )
                    if hour_data:
                        if has_positive_actuals:
                            movement_by_date[movement_date] = {
                                str(hour_key): sorted_hour_entry
                                for hour_key, sorted_hour_entry in sorted(
                                    hour_data.items()
                                )
                            }
                            continue
            available_dates = sorted(movement_by_date.keys())
            selected_date = (
                available_dates[-1]
                if available_dates
                else (
                    requested_dates[-1]
                    if requested_dates
                    else ha_today(context.HASS).isoformat()
                )
            )
            return web.json_response(
                {
                    "success": True,
                    "dates": {
                        response_date: movement_by_date[response_date]
                        for response_date in available_dates
                    },
                    "available_dates": available_dates,
                    "selected_date": selected_date,
                    "groups": sorted(group_names),
                    "time_context": _time_context(),
                }
            )
        except Exception as request_error:
            _LOGGER.error("Error getting shadow movement: %s", request_error)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )
