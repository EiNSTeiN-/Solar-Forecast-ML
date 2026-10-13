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


class ShadowMovementView(HomeAssistantView):

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
            _var_var_33 = request.query.get("days")
            _var_var_34 = request.query.get("date")
            if _var_var_33:
                _var_var_35 = min(max(int(_var_var_33), 1), 30)
                _var_var_36 = ha_today(context.HASS)
                _var_var_37 = _var_var_36 - timedelta(days=_var_var_35 - 1)
                _var_var_39 = [
                    (_var_var_37 + timedelta(days=_var_var_38)).isoformat()
                    for _var_var_38 in range(_var_var_35)
                ]
            else:
                _var_var_39 = [_var_var_34 or ha_today(context.HASS).isoformat()]
            _var_var_40 = {}
            _var_var_41 = set()
            async with _get_db() as _var_var_15:
                _var_var_32 = await _table_columns(
                    _var_var_15, "prediction_panel_groups"
                )
                _var_var_31 = await _table_columns(_var_var_15, "hourly_predictions")

                def hp_expr(name: str, default: str) -> str:
                    return (
                        f"hp.{name}" if name in _var_var_31 else f"{default} AS {name}"
                    )

                def ppg_expr(name: str, default: str) -> str:
                    return (
                        f"ppg.{name}" if name in _var_var_32 else f"{default} AS {name}"
                    )

                for _var_var_44 in _var_var_39:
                    _var_var_45 = {}
                    async with _var_var_15.execute(
                        f"SELECT ppg.prediction_id, ppg.group_name,\n                                  ppg.prediction_kwh, ppg.actual_kwh,\n                                  hp.target_hour,\n                                  {hp_expr('exclude_from_learning', '0')},\n                                  {hp_expr('exclude_from_clean_evaluation', '0')},\n                                  {hp_expr('mppt_throttled', '0')},\n                                  {hp_expr('inverter_clipped', '0')},\n                                  {hp_expr('mppt_throttle_reason', 'NULL')},\n                                  {hp_expr('has_weather_alert', '0')},\n                                  {hp_expr('weather_alert_type', 'NULL')},\n                                  {ppg_expr('exclude_from_learning_group', '0')},\n                                  {ppg_expr('exclusion_reason_group', 'NULL')},\n                                  {ppg_expr('snow_covered_group', '0')},\n                                  {ppg_expr('topology_compatible', '1')},\n                                  {ppg_expr('topology_exclusion_reason', 'NULL')}\n                           FROM prediction_panel_groups ppg\n                           JOIN hourly_predictions hp ON hp.prediction_id = ppg.prediction_id\n                           WHERE hp.target_date = ?\n                           ORDER BY hp.target_hour, ppg.group_name",
                        (_var_var_44,),
                    ) as _var_var_7:
                        _var_var_46 = await _var_var_7.fetchall()
                        for _var_var_8 in _var_var_46:
                            _var_var_47 = _var_var_8["target_hour"]
                            if _var_var_47 < 6 or _var_var_47 > 20:
                                continue
                            if _var_var_47 not in _var_var_45:
                                _var_var_45[_var_var_47] = {"groups": {}}
                            _var_var_48 = _var_var_8["group_name"]
                            _var_var_49 = _panel_group_exclusion_reason(_var_var_8)
                            _var_var_45[_var_var_47]["groups"][_var_var_48] = {
                                "prediction": round(
                                    _var_var_8["prediction_kwh"] or 0, 4
                                ),
                                "actual": (
                                    None
                                    if _var_var_8["actual_kwh"] is None
                                    else round(_var_var_8["actual_kwh"], 4)
                                ),
                                "excluded": _var_var_49 is not None,
                                "exclusion_reason": _var_var_49,
                            }
                    async with _var_var_15.execute(
                        "SELECT acpg.hour, acpg.group_name,\n                                  acpg.theoretical_kwh, acpg.poa_wm2\n                           FROM astronomy_cache_panel_groups acpg\n                           WHERE acpg.cache_date = ?\n                             AND acpg.hour BETWEEN 6 AND 20\n                           ORDER BY acpg.hour, acpg.group_name",
                        (_var_var_44,),
                    ) as _var_var_7:
                        _var_var_46 = await _var_var_7.fetchall()
                        for _var_var_8 in _var_var_46:
                            _var_var_47 = _var_var_8["hour"]
                            if _var_var_47 not in _var_var_45:
                                _var_var_45[_var_var_47] = {"groups": {}}
                            _var_var_48 = _var_var_8["group_name"]
                            if _var_var_48 not in _var_var_45[_var_var_47]["groups"]:
                                _var_var_45[_var_var_47]["groups"][_var_var_48] = {}
                            _var_var_45[_var_var_47]["groups"][_var_var_48][
                                "theoretical"
                            ] = round(_var_var_8["theoretical_kwh"] or 0, 4)
                            _var_var_45[_var_var_47]["groups"][_var_var_48]["poa"] = (
                                round(_var_var_8["poa_wm2"] or 0, 1)
                            )
                    async with _var_var_15.execute(
                        "SELECT p.target_hour,\n                                  h.shadow_percent, h.shadow_type, h.root_cause,\n                                  h.efficiency_ratio, h.loss_kwh, h.confidence\n                           FROM hourly_shadow_detection h\n                           JOIN hourly_predictions p ON h.prediction_id = p.prediction_id\n                           WHERE p.target_date = ?\n                             AND p.target_hour BETWEEN 6 AND 20\n                           ORDER BY p.target_hour",
                        (_var_var_44,),
                    ) as _var_var_7:
                        _var_var_46 = await _var_var_7.fetchall()
                        for _var_var_8 in _var_var_46:
                            _var_var_47 = _var_var_8["target_hour"]
                            if _var_var_47 not in _var_var_45:
                                _var_var_45[_var_var_47] = {"groups": {}}
                            _var_var_45[_var_var_47]["shadow"] = {
                                "pct": round(_var_var_8["shadow_percent"] or 0, 1),
                                "type": _var_var_8["shadow_type"] or "none",
                                "cause": _var_var_8["root_cause"] or "unknown",
                                "confidence": round(_var_var_8["confidence"] or 0, 2),
                                "local_shadow_confirmed": is_local_shadow_observation(
                                    _var_var_8["root_cause"], _var_var_8["confidence"]
                                ),
                                "efficiency": round(
                                    (_var_var_8["efficiency_ratio"] or 1) * 100, 1
                                ),
                                "loss": round(_var_var_8["loss_kwh"] or 0, 3),
                            }
                    _var_var_50 = {
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
                    for _var_var_47, _var_var_51 in _var_var_45.items():
                        _var_var_52 = 0.0
                        _var_var_53 = 0.0
                        _var_var_54 = 0
                        _var_var_55 = 0
                        _var_var_56 = 0
                        _var_var_57 = {}
                        for _var_var_48, _var_var_58 in _var_var_51.get(
                            "groups", {}
                        ).items():
                            _var_var_59 = _var_var_58.get("theoretical", 0)
                            _var_var_60 = _var_var_58.get("actual")
                            if _var_var_58.get("excluded"):
                                _var_var_58["efficiency"] = None
                                _var_var_58["shadow_pct"] = None
                                _var_var_55 += 1
                                _var_var_61 = (
                                    _var_var_58.get("exclusion_reason") or "excluded"
                                )
                                _var_var_57[_var_var_61] = (
                                    _var_var_57.get(_var_var_61, 0) + 1
                                )
                                if _var_var_61 in _var_var_50:
                                    _var_var_56 += 1
                                    continue
                                continue
                            if _var_var_60 is not None:
                                if _var_var_59 > 0.001:
                                    _var_var_58["efficiency"] = round(
                                        min(_var_var_60 / _var_var_59 * 100, 100), 1
                                    )
                                    _var_var_58["shadow_pct"] = round(
                                        max(
                                            0,
                                            min(
                                                (1 - _var_var_60 / _var_var_59) * 100,
                                                100,
                                            ),
                                        ),
                                        1,
                                    )
                                    _var_var_52 += max(float(_var_var_60), 0.0)
                                    _var_var_53 += max(float(_var_var_59), 0.0)
                                    _var_var_54 += 1
                                    continue
                            if _var_var_60 is None:
                                _var_var_58["efficiency"] = None
                                _var_var_58["shadow_pct"] = None
                                continue
                            _var_var_58["efficiency"] = 0
                            _var_var_58["shadow_pct"] = 0
                            continue
                        if _var_var_53 > 0.001 and _var_var_54 > 0:
                            _var_var_62 = min(_var_var_52 / _var_var_53 * 100, 100)
                            _var_var_63 = max(0, min(100, 100 - _var_var_62))
                            _var_var_64 = _var_var_51.get("shadow") or {}
                            _var_var_51["clean_shadow"] = {
                                "pct": round(_var_var_63, 1),
                                "efficiency": round(_var_var_62, 1),
                                "loss": round(max(_var_var_53 - _var_var_52, 0), 3),
                                "cause": _var_var_64.get("cause") or "unknown",
                                "local_shadow_confirmed": bool(
                                    _var_var_64.get("local_shadow_confirmed")
                                ),
                                "valid_groups": _var_var_54,
                                "excluded_groups": _var_var_55,
                                "throttled_groups": _var_var_56,
                                "exclusion_reasons": _var_var_57,
                            }
                            continue
                        _var_var_51["clean_shadow"] = {
                            "pct": 0,
                            "efficiency": None,
                            "loss": 0,
                            "cause": (
                                "mppt_throttled"
                                if _var_var_56
                                else "excluded" if _var_var_55 else "unknown"
                            ),
                            "valid_groups": 0,
                            "excluded_groups": _var_var_55,
                            "throttled_groups": _var_var_56,
                            "exclusion_reasons": _var_var_57,
                        }
                        continue
                    for _var_var_51 in _var_var_45.values():
                        _var_var_41.update(_var_var_51.get("groups", {}).keys())
                    _var_var_65 = any(
                        (
                            (_var_var_71.get("actual") or 0) > 0
                            for _var_var_70 in _var_var_45.values()
                            for _var_var_71 in _var_var_70.get("groups", {}).values()
                        )
                    )
                    if _var_var_45:
                        if _var_var_65:
                            _var_var_40[_var_var_44] = {
                                str(_var_var_66): _var_var_67
                                for _var_var_66, _var_var_67 in sorted(
                                    _var_var_45.items()
                                )
                            }
                            continue
            _var_var_68 = sorted(_var_var_40.keys())
            _var_var_69 = (
                _var_var_68[-1]
                if _var_var_68
                else (
                    _var_var_39[-1]
                    if _var_var_39
                    else ha_today(context.HASS).isoformat()
                )
            )
            return web.json_response(
                {
                    "success": True,
                    "dates": {
                        _var_var_17: _var_var_40[_var_var_17]
                        for _var_var_17 in _var_var_68
                    },
                    "available_dates": _var_var_68,
                    "selected_date": _var_var_69,
                    "groups": sorted(_var_var_41),
                    "time_context": _time_context(),
                }
            )
        except Exception as _var_var_12:
            _LOGGER.error("Error getting shadow movement: %s", _var_var_12)
            return web.json_response(
                {"success": False, "error": "Internal server error"}, status=500
            )
