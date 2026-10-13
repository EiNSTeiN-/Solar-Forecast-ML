# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
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


class ShadowFingerprintView(HomeAssistantView):

    @local_only
    async def get(self, request: web.Request) -> web.Response:
        """Return hour-of-day + month-of-year shadow pattern data. @zara"""
        if not is_dev_active(context.HASS):
            return json_dev_required()
        try:
            if context.HASS is None:
                return web.json_response({"error": "not configured"}, status=404)
            _var_var_0 = request.query.get("group", "_system_")
            _var_var_1 = []
            _var_var_2 = []
            _var_var_3 = []
            _var_var_4 = None
            _var_var_5 = "unknown"
            try:
                async with _get_db() as _var_var_6:
                    try:
                        async with _var_var_6.execute(
                            "SELECT version FROM shadow_pattern_config WHERE id = 1"
                        ) as _var_var_7:
                            _var_var_8 = await _var_var_7.fetchone()
                            if _var_var_8:
                                _var_var_5 = str(_var_var_8[0] or "unknown")
                    except Exception as _var_var_9:
                        _LOGGER.debug(
                            "shadow_pattern_config unavailable: %s", _var_var_9
                        )
                    try:
                        async with _var_var_6.execute(
                            "SELECT MIN(date) FROM shadow_learning_history WHERE group_name = ?",
                            (_var_var_0,),
                        ) as _var_var_7:
                            _var_var_8 = await _var_var_7.fetchone()
                            if _var_var_8 and _var_var_8[0]:
                                _var_var_4 = str(_var_var_8[0])
                    except Exception as _var_var_9:
                        _LOGGER.debug(
                            "shadow_learning_history unavailable: %s", _var_var_9
                        )
                    try:
                        async with _var_var_6.execute(
                            "SELECT DISTINCT group_name FROM shadow_pattern_hourly UNION SELECT DISTINCT group_name FROM shadow_pattern_seasonal"
                        ) as _var_var_7:
                            for _var_var_10 in await _var_var_7.fetchall():
                                if _var_var_10[0]:
                                    _var_var_3.append(_var_var_10[0])
                    except Exception:
                        pass
                    try:
                        async with _var_var_6.execute(
                            "SELECT hour, shadow_occurrence_rate, avg_shadow_percent,\n                                      pattern_type, confidence, sample_count, first_learned,\n                                      pct_building_tree, pct_weather_clouds, pct_low_sun, pct_other\n                               FROM shadow_pattern_hourly\n                               WHERE group_name = ?\n                               ORDER BY hour",
                            (_var_var_0,),
                        ) as _var_var_7:
                            for _var_var_10 in await _var_var_7.fetchall():
                                _var_var_1.append(
                                    {
                                        "hour": _var_var_10[0],
                                        "shadow_occurrence_rate": _var_var_10[1],
                                        "avg_shadow_percent": _var_var_10[2],
                                        "pattern_type": _var_var_10[3],
                                        "confidence": _var_var_10[4],
                                        "sample_count": _var_var_10[5],
                                        "first_learned": _var_var_10[6],
                                        "pct_building_tree": _var_var_10[7],
                                        "pct_weather_clouds": _var_var_10[8],
                                        "pct_low_sun": _var_var_10[9],
                                        "pct_other": _var_var_10[10],
                                    }
                                )
                    except Exception as _var_var_9:
                        _LOGGER.debug(
                            "shadow_pattern_hourly unavailable: %s", _var_var_9
                        )
                    try:
                        async with _var_var_6.execute(
                            "SELECT month, hour, shadow_occurrence_rate, avg_shadow_percent,\n                                      dominant_cause, sample_count, confidence\n                               FROM shadow_pattern_seasonal\n                               WHERE group_name = ?\n                               ORDER BY month, hour",
                            (_var_var_0,),
                        ) as _var_var_7:
                            for _var_var_10 in await _var_var_7.fetchall():
                                _var_var_2.append(
                                    {
                                        "month": _var_var_10[0],
                                        "hour": _var_var_10[1],
                                        "shadow_occurrence_rate": _var_var_10[2],
                                        "avg_shadow_percent": _var_var_10[3],
                                        "dominant_cause": _var_var_10[4],
                                        "sample_count": _var_var_10[5],
                                        "confidence": _var_var_10[6],
                                    }
                                )
                    except Exception as _var_var_9:
                        _LOGGER.debug(
                            "shadow_pattern_seasonal unavailable: %s", _var_var_9
                        )
            except Exception as _var_var_9:
                _LOGGER.debug("Shadow fingerprint lookup failed: %s", _var_var_9)
            _var_var_11 = build_shadow_fingerprint(
                _var_var_1 if _var_var_5 == "2.0" else [],
                _var_var_2 if _var_var_5 == "2.0" else [],
                history_since=_var_var_4,
            )
            return web.json_response(
                {
                    "success": True,
                    "group": _var_var_0,
                    "available_groups": _var_var_3,
                    "model_version": _var_var_5,
                    "model_ready": _var_var_5 == "2.0",
                    **_var_var_11,
                    "time_context": _time_context(),
                }
            )
        except Exception as _var_var_12:
            _LOGGER.error("Error in shadow fingerprint: %s", _var_var_12, exc_info=True)
            return web.json_response({"error": str(_var_var_12)}, status=500)


class ShadowAnalyticsView(HomeAssistantView):

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
            _var_var_13 = int(request.query.get("days", "30"))
            _var_var_13 = min(max(_var_var_13, 7), 365)
            _var_var_14 = (
                ha_today(context.HASS) - timedelta(days=_var_var_13)
            ).isoformat()
            async with _get_db() as _var_var_15:
                _var_var_16 = {}
                async with _var_var_15.execute(
                    "SELECT p.target_date, p.target_hour,\n                              h.shadow_percent, h.shadow_type, h.root_cause,\n                              h.efficiency_ratio, h.loss_kwh, h.confidence\n                       FROM hourly_shadow_detection h\n                       JOIN hourly_predictions p ON h.prediction_id = p.prediction_id\n                       WHERE p.target_date >= ?\n                         AND p.target_hour >= 7 AND p.target_hour <= 18\n                       ORDER BY p.target_date, p.target_hour",
                    (_var_var_14,),
                ) as _var_var_7:
                    for _var_var_8 in await _var_var_7.fetchall():
                        _var_var_17 = _var_var_8["target_date"]
                        _var_var_18 = _var_var_8["target_hour"]
                        if _var_var_17 not in _var_var_16:
                            _var_var_16[_var_var_17] = {}
                        _var_var_16[_var_var_17][_var_var_18] = {
                            "pct": round(_var_var_8["shadow_percent"] or 0, 1),
                            "type": _var_var_8["shadow_type"],
                            "cause": _var_var_8["root_cause"],
                            "eff": round(_var_var_8["efficiency_ratio"] or 0, 2),
                            "loss": round(_var_var_8["loss_kwh"] or 0, 3),
                        }
                _var_var_19 = {}
                async with _var_var_15.execute(
                    "SELECT h.root_cause, COUNT(*) as cnt\n                       FROM hourly_shadow_detection h\n                       JOIN hourly_predictions p ON h.prediction_id = p.prediction_id\n                       WHERE p.target_date >= ?\n                         AND p.target_hour >= 7 AND p.target_hour <= 18\n                         AND h.shadow_type NOT IN ('none', 'not_evaluable', 'night')\n                         AND h.root_cause IN (\n                             'building_tree_obstruction', 'possible_obstruction'\n                         )\n                         AND COALESCE(p.mppt_throttled, 0) = 0\n                         AND COALESCE(p.inverter_clipped, 0) = 0\n                         AND COALESCE(p.exclude_from_clean_evaluation, 0) = 0\n                       GROUP BY h.root_cause\n                       ORDER BY cnt DESC",
                    (_var_var_14,),
                ) as _var_var_7:
                    for _var_var_8 in await _var_var_7.fetchall():
                        _var_var_20 = _var_var_8["root_cause"] or "unknown"
                        if _var_var_20 != "night":
                            _var_var_19[_var_var_20] = _var_var_8["cnt"]
                _var_var_21 = []
                async with _var_var_15.execute(
                    "SELECT date, shadow_hours_count, cumulative_loss_kwh\n                       FROM daily_summary_shadow_analysis\n                       WHERE date >= ?\n                       ORDER BY date",
                    (_var_var_14,),
                ) as _var_var_7:
                    for _var_var_8 in await _var_var_7.fetchall():
                        _var_var_21.append(
                            {
                                "date": _var_var_8["date"],
                                "hours": _var_var_8["shadow_hours_count"],
                                "loss_kwh": round(
                                    _var_var_8["cumulative_loss_kwh"] or 0, 3
                                ),
                            }
                        )
                _var_var_22 = sum(
                    (_var_var_17["loss_kwh"] for _var_var_17 in _var_var_21)
                )
                _var_var_23 = sum((_var_var_17["hours"] for _var_var_17 in _var_var_21))
                _var_var_24 = sum(
                    (1 for _var_var_17 in _var_var_21 if _var_var_17["hours"] > 0)
                )
                _var_var_25 = None
                async with _var_var_15.execute(
                    "SELECT AVG(h.efficiency_ratio) as avg_eff\n                       FROM hourly_shadow_detection h\n                       JOIN hourly_predictions p ON h.prediction_id = p.prediction_id\n                       WHERE p.target_date >= ?\n                         AND p.target_hour >= 7 AND p.target_hour <= 18\n                         AND h.shadow_type NOT IN ('none', 'not_evaluable', 'night')\n                         AND h.root_cause IN (\n                             'building_tree_obstruction', 'possible_obstruction'\n                         )\n                         AND COALESCE(p.mppt_throttled, 0) = 0\n                         AND COALESCE(p.inverter_clipped, 0) = 0\n                         AND COALESCE(p.exclude_from_clean_evaluation, 0) = 0",
                    (_var_var_14,),
                ) as _var_var_7:
                    _var_var_8 = await _var_var_7.fetchone()
                    if _var_var_8 and _var_var_8["avg_eff"] is not None:
                        _var_var_25 = round(_var_var_8["avg_eff"], 3)
                _var_var_26 = (
                    max(_var_var_19, key=_var_var_19.get) if _var_var_19 else None
                )
                _var_var_27 = {}
                async with _var_var_15.execute(
                    "SELECT p.target_hour,\n                              AVG(h.shadow_percent) as avg_pct,\n                              COUNT(*) as total,\n                              SUM(CASE WHEN h.shadow_type <> 'none' THEN 1 ELSE 0 END) as shadow_cnt\n                       FROM hourly_shadow_detection h\n                       JOIN hourly_predictions p ON h.prediction_id = p.prediction_id\n                       WHERE p.target_date >= ?\n                         AND p.target_hour >= 7 AND p.target_hour <= 18\n                         AND h.shadow_type NOT IN ('not_evaluable', 'night')\n                         AND (\n                             h.shadow_type = 'none'\n                             OR h.root_cause IN (\n                                 'building_tree_obstruction', 'possible_obstruction'\n                             )\n                         )\n                         AND COALESCE(p.mppt_throttled, 0) = 0\n                         AND COALESCE(p.inverter_clipped, 0) = 0\n                         AND COALESCE(p.exclude_from_clean_evaluation, 0) = 0\n                       GROUP BY p.target_hour\n                       ORDER BY p.target_hour",
                    (_var_var_14,),
                ) as _var_var_7:
                    for _var_var_8 in await _var_var_7.fetchall():
                        _var_var_18 = _var_var_8["target_hour"]
                        _var_var_27[_var_var_18] = {
                            "avg_pct": round(_var_var_8["avg_pct"] or 0, 1),
                            "occurrence_rate": (
                                round(
                                    _var_var_8["shadow_cnt"]
                                    / _var_var_8["total"]
                                    * 100,
                                    1,
                                )
                                if _var_var_8["total"] > 0
                                else 0
                            ),
                        }
                _var_var_28 = {}
                async with _var_var_15.execute(
                    "SELECT total_days_learned, total_hours_learned,\n                              last_learning_date, patterns_detected\n                         FROM shadow_pattern_config WHERE id = 1"
                ) as _var_var_7:
                    _var_var_8 = await _var_var_7.fetchone()
                    if _var_var_8:
                        _var_var_28 = {
                            "days_learned": _var_var_8["total_days_learned"],
                            "hours_learned": _var_var_8["total_hours_learned"],
                            "last_date": _var_var_8["last_learning_date"],
                            "patterns_detected": _var_var_8["patterns_detected"],
                        }
            return web.json_response(
                {
                    "success": True,
                    "data": {
                        "stats": {
                            "total_loss_kwh": round(_var_var_22, 2),
                            "shadow_hours": _var_var_23,
                            "days_with_shadow": _var_var_24,
                            "days_analyzed": len(_var_var_21),
                            "avg_efficiency": _var_var_25,
                            "dominant_cause": _var_var_26,
                        },
                        "heatmap": _var_var_16,
                        "causes": _var_var_19,
                        "daily_loss": _var_var_21,
                        "hourly_pattern": _var_var_27,
                        "learning": _var_var_28,
                    },
                }
            )
        except Exception as _var_var_12:
            _LOGGER.error("Error getting shadow analytics: %s", _var_var_12)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )
