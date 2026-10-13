# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager


class JsonMigrator:

    async def _migrate_dni_tracker(self, data: Dict[str, Any]) -> None:
        _var_var_47 = data.get("max_dni", {})
        _var_var_48 = data.get("history", {})
        _var_var_49 = data.get("last_updated")
        _var_var_28 = data.get("version", "1.0")
        if not _var_var_49:
            self.stats.skipped += 1
            return
        try:
            _var_var_50 = datetime.fromisoformat(_var_var_49).date()
        except Exception:
            self.stats.skipped += 1
            return
        for _var_var_51, _var_var_52 in _var_var_47.items():
            try:
                _var_var_53 = int(_var_var_51)
                if _var_var_53 < 6 or _var_var_53 > 20:
                    continue
                _var_var_33 = await self.db.fetchone(
                    "SELECT id FROM ai_dni_tracker WHERE hour = ?", (_var_var_53,)
                )
                if _var_var_33:
                    await self.db.execute(
                        "UPDATE ai_dni_tracker SET max_dni = ?, version = ?, last_updated = ? WHERE hour = ?",
                        (_var_var_52, _var_var_28, _var_var_50, _var_var_53),
                    )
                    self.stats.updated += 1
                else:
                    await self.db.execute(
                        "INSERT INTO ai_dni_tracker (hour, max_dni, version, last_updated) VALUES (?, ?, ?, ?)",
                        (_var_var_53, _var_var_52, _var_var_28, _var_var_50),
                    )
                    self.stats.imported += 1
                if _var_var_51 in _var_var_48:
                    for _var_var_54 in _var_var_48[_var_var_51]:
                        await self.db.execute(
                            "INSERT OR IGNORE INTO ai_dni_history (hour, dni_value) VALUES (?, ?)",
                            (_var_var_53, _var_var_54),
                        )
            except Exception as _var_var_7:
                _LOGGER.debug(f"Skip DNI hour {_var_var_51}: {_var_var_7}")
                self.stats.skipped += 1

    async def _migrate_learned_weights(self, data: Dict[str, Any]) -> None:
        try:
            _var_var_33 = await self.db.fetchone(
                "SELECT id FROM ai_learned_weights_meta WHERE id = 1"
            )
            _var_var_55 = (
                data.get("version", "2.0"),
                data.get("active_model", "tiny_lstm"),
                data.get("training_samples"),
                data.get("last_trained"),
                data.get("accuracy"),
                data.get("rmse"),
                datetime.now(),
            )
            if _var_var_33:
                await self.db.execute(
                    "UPDATE ai_learned_weights_meta SET version = ?, active_model = ?, training_samples = ?, last_trained = ?, accuracy = ?, rmse = ?, updated_at = ? WHERE id = 1",
                    _var_var_55,
                )
                self.stats.updated += 1
            else:
                await self.db.execute(
                    "INSERT INTO ai_learned_weights_meta (id, version, active_model, training_samples, last_trained, accuracy, rmse, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                    _var_var_55,
                )
                self.stats.imported += 1
            _var_var_56 = data.get("ridge", {})
            if _var_var_56:
                _var_var_57 = _var_var_56.get("weights", [])
                if _var_var_57 and isinstance(_var_var_57, list):
                    for _var_var_58, _var_var_59 in enumerate(_var_var_57):
                        if isinstance(_var_var_59, list):
                            for _var_var_60, _var_var_11 in enumerate(_var_var_59):
                                if _var_var_11 is not None and _var_var_11 != 0:
                                    await self.db.execute(
                                        "INSERT OR REPLACE INTO ai_ridge_weights (row_index, col_index, weight_value) VALUES (?, ?, ?)",
                                        (_var_var_58, _var_var_60, _var_var_11),
                                    )
                                    self.stats.imported += 1
                _var_var_61 = await self.db.fetchone(
                    "SELECT id FROM ai_ridge_meta WHERE id = 1"
                )
                _var_var_62 = (
                    _var_var_56.get("model_type", "TinyRidge"),
                    _var_var_56.get("alpha"),
                    _var_var_56.get("input_size"),
                    _var_var_56.get("hidden_size"),
                    _var_var_56.get("sequence_length"),
                    _var_var_56.get("num_outputs"),
                    _var_var_56.get("flat_size"),
                    _var_var_56.get("trained_samples"),
                    _var_var_56.get("loo_cv_score"),
                    _var_var_56.get("accuracy"),
                    _var_var_56.get("rmse"),
                    datetime.now(),
                )
                if _var_var_61:
                    await self.db.execute(
                        "UPDATE ai_ridge_meta SET model_type = ?, alpha = ?, input_size = ?, hidden_size = ?, sequence_length = ?, num_outputs = ?, flat_size = ?, trained_samples = ?, loo_cv_score = ?, accuracy = ?, rmse = ?, updated_at = ? WHERE id = 1",
                        _var_var_62,
                    )
                    self.stats.updated += 1
                else:
                    await self.db.execute(
                        "INSERT INTO ai_ridge_meta (id, model_type, alpha, input_size, hidden_size, sequence_length, num_outputs, flat_size, trained_samples, loo_cv_score, accuracy, rmse, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        _var_var_62,
                    )
                    self.stats.imported += 1
                _var_var_63 = _var_var_56.get("feature_means", [])
                _var_var_64 = _var_var_56.get("feature_stds", [])
                if _var_var_63 and _var_var_64:
                    for _var_var_17, (_var_var_65, _var_var_66) in enumerate(
                        zip(_var_var_63, _var_var_64)
                    ):
                        if _var_var_65 is not None:
                            if _var_var_66 is not None:
                                await self.db.execute(
                                    "INSERT OR REPLACE INTO ai_ridge_normalization (feature_index, feature_mean, feature_std) VALUES (?, ?, ?)",
                                    (_var_var_17, _var_var_65, _var_var_66),
                                )
                                self.stats.imported += 1
            _var_var_67 = data.get("lstm", {})
            if _var_var_67:
                _var_var_68 = [
                    "Wf",
                    "Wi",
                    "Wc",
                    "Wo",
                    "bf",
                    "bi",
                    "bc",
                    "bo",
                    "Wy",
                    "by",
                    "W_query",
                    "W_key",
                    "W_value",
                    "W_attn_out",
                    "b_attn_out",
                ]
                for _var_var_69 in _var_var_68:
                    _var_var_70 = _var_var_67.get(_var_var_69, [])
                    if isinstance(_var_var_70, list):
                        _var_var_71 = 0
                        for _var_var_59 in _var_var_70:
                            if isinstance(_var_var_59, list):
                                for _var_var_11 in _var_var_59:
                                    if isinstance(_var_var_11, (int, float)):
                                        await self.db.execute(
                                            "INSERT OR REPLACE INTO ai_lstm_weights (weight_type, weight_index, weight_value) VALUES (?, ?, ?)",
                                            (_var_var_69, _var_var_71, _var_var_11),
                                        )
                                        self.stats.imported += 1
                                        _var_var_71 += 1
                            elif isinstance(_var_var_59, (int, float)):
                                await self.db.execute(
                                    "INSERT OR REPLACE INTO ai_lstm_weights (weight_type, weight_index, weight_value) VALUES (?, ?, ?)",
                                    (_var_var_69, _var_var_71, _var_var_59),
                                )
                                self.stats.imported += 1
                                _var_var_71 += 1
                _var_var_72 = await self.db.fetchone(
                    "SELECT id FROM ai_lstm_meta WHERE id = 1"
                )
                _var_var_73 = (
                    _var_var_67.get("input_size"),
                    _var_var_67.get("hidden_size"),
                    _var_var_67.get("sequence_length"),
                    _var_var_67.get("num_outputs"),
                    _var_var_67.get("has_attention", False),
                    _var_var_67.get("training_samples") or data.get("training_samples"),
                    _var_var_67.get("accuracy") or data.get("accuracy"),
                    _var_var_67.get("rmse") or data.get("rmse"),
                    datetime.now(),
                )
                if _var_var_72:
                    await self.db.execute(
                        "UPDATE ai_lstm_meta SET input_size = ?, hidden_size = ?, sequence_length = ?, num_outputs = ?, has_attention = ?, training_samples = ?, accuracy = ?, rmse = ?, updated_at = ? WHERE id = 1",
                        _var_var_73,
                    )
                    self.stats.updated += 1
                else:
                    await self.db.execute(
                        "INSERT INTO ai_lstm_meta (id, input_size, hidden_size, sequence_length, num_outputs, has_attention, training_samples, accuracy, rmse, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        _var_var_73,
                    )
                    self.stats.imported += 1
        except Exception as _var_var_7:
            _LOGGER.debug(f"Skip learned weights: {_var_var_7}")
            self.stats.skipped += 1
        return None if True else None

    async def _migrate_learning_config(self, data: Dict[str, Any]) -> None:
        try:
            _var_var_88 = data.get("physics_defaults", {})
            _var_var_89 = data.get("metadata", {})
            _var_var_90 = data.get("updated_at")
            if _var_var_90:
                try:
                    _var_var_90 = datetime.fromisoformat(_var_var_90)
                except Exception:
                    _var_var_90 = datetime.now()
            else:
                _var_var_90 = datetime.now()
            _var_var_33 = await self.db.fetchone(
                "SELECT id FROM physics_learning_config WHERE id = 1"
            )
            if _var_var_33:
                await self.db.execute(
                    "UPDATE physics_learning_config SET version = ?, albedo = ?, system_efficiency = ?, learned_efficiency_factor = ?, rolling_window_days = ?, min_samples = ?, updated_at = ? WHERE id = 1",
                    (
                        data.get("version", "3.0"),
                        _var_var_88.get("albedo", 0.2),
                        _var_var_88.get("system_efficiency", 0.9),
                        _var_var_88.get("learned_efficiency_factor", 1.0),
                        _var_var_89.get("rolling_window_days", 21),
                        _var_var_89.get("min_samples", 1),
                        _var_var_90,
                    ),
                )
                self.stats.updated += 1
            else:
                await self.db.execute(
                    "INSERT INTO physics_learning_config (id, version, albedo, system_efficiency, learned_efficiency_factor, rolling_window_days, min_samples, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        data.get("version", "3.0"),
                        _var_var_88.get("albedo", 0.2),
                        _var_var_88.get("system_efficiency", 0.9),
                        _var_var_88.get("learned_efficiency_factor", 1.0),
                        _var_var_89.get("rolling_window_days", 21),
                        _var_var_89.get("min_samples", 1),
                        _var_var_90,
                    ),
                )
                self.stats.imported += 1
            for _var_var_79, _var_var_80 in data.get("group_calibration", {}).items():
                await self._migrate_group_calibration(_var_var_79, _var_var_80)
        except Exception as _var_var_7:
            _LOGGER.debug(f"Skip learning config: {_var_var_7}")
            self.stats.skipped += 1
        return None if True else None

    async def _migrate_group_calibration(
        self, group_name: str, group_data: Dict[str, Any]
    ) -> None:
        try:
            _var_var_49 = group_data.get("last_updated")
            if _var_var_49:
                try:
                    _var_var_49 = datetime.fromisoformat(_var_var_49)
                except Exception:
                    _var_var_49 = datetime.now()
            else:
                _var_var_49 = datetime.now()
            _var_var_33 = await self.db.fetchone(
                "SELECT id FROM physics_calibration_groups WHERE group_name = ?",
                (group_name,),
            )
            if _var_var_33:
                await self.db.execute(
                    "UPDATE physics_calibration_groups SET global_factor = ?, sample_count = ?, confidence = ?, last_updated = ? WHERE group_name = ?",
                    (
                        group_data.get("global_factor", 1.0),
                        group_data.get("sample_count", 0),
                        group_data.get("confidence", 0.0),
                        _var_var_49,
                        group_name,
                    ),
                )
                self.stats.updated += 1
            else:
                await self.db.execute(
                    "INSERT INTO physics_calibration_groups (group_name, global_factor, sample_count, confidence, last_updated) VALUES (?, ?, ?, ?, ?)",
                    (
                        group_name,
                        group_data.get("global_factor", 1.0),
                        group_data.get("sample_count", 0),
                        group_data.get("confidence", 0.0),
                        _var_var_49,
                    ),
                )
                self.stats.imported += 1
            for _var_var_51, _var_var_30 in group_data.get(
                "hourly_factors", {}
            ).items():
                try:
                    _var_var_53 = int(_var_var_51)
                    await self.db.execute(
                        "INSERT OR REPLACE INTO physics_calibration_hourly (group_name, hour, factor) VALUES (?, ?, ?)",
                        (group_name, _var_var_53, _var_var_30),
                    )
                except Exception:
                    continue
            for _var_var_84, _var_var_85 in group_data.get(
                "bucket_factors", {}
            ).items():
                await self.db.execute(
                    "INSERT OR REPLACE INTO physics_calibration_buckets (group_name, bucket_name, global_factor, sample_count, confidence) VALUES (?, ?, ?, ?, ?)",
                    (
                        group_name,
                        _var_var_84,
                        _var_var_85.get("global_factor", 1.0),
                        _var_var_85.get("sample_count", 0),
                        _var_var_85.get("confidence", 0.0),
                    ),
                )
                for _var_var_51, _var_var_30 in _var_var_85.get(
                    "hourly_factors", {}
                ).items():
                    try:
                        _var_var_53 = int(_var_var_51)
                        await self.db.execute(
                            "INSERT OR REPLACE INTO physics_calibration_bucket_hourly (group_name, bucket_name, hour, factor) VALUES (?, ?, ?, ?)",
                            (group_name, _var_var_84, _var_var_53, _var_var_30),
                        )
                    except Exception:
                        continue
        except Exception as _var_var_7:
            _LOGGER.debug(f"Skip group calibration {group_name}: {_var_var_7}")
        return None if True else None

    async def _migrate_coordinator_state(self, data: Dict[str, Any]) -> None:
        try:
            _var_var_91 = data.get("last_set_date")
            _var_var_49 = data.get("last_updated")
            if _var_var_91:
                try:
                    _var_var_91 = datetime.fromisoformat(_var_var_91).date()
                except Exception:
                    _var_var_91 = None
            if _var_var_49:
                try:
                    _var_var_49 = datetime.fromisoformat(_var_var_49)
                except Exception:
                    _var_var_49 = datetime.now()
            else:
                _var_var_49 = datetime.now()
            _var_var_33 = await self.db.fetchone(
                "SELECT id FROM coordinator_state WHERE id = 1"
            )
            if _var_var_33:
                await self.db.execute(
                    "UPDATE coordinator_state SET expected_daily_production = ?, last_set_date = ?, last_updated = ? WHERE id = 1",
                    (data.get("expected_daily_production"), _var_var_91, _var_var_49),
                )
                self.stats.updated += 1
            else:
                await self.db.execute(
                    "INSERT INTO coordinator_state (id, expected_daily_production, last_set_date, last_updated) VALUES (1, ?, ?, ?)",
                    (data.get("expected_daily_production"), _var_var_91, _var_var_49),
                )
                self.stats.imported += 1
        except Exception as _var_var_7:
            _LOGGER.debug(f"Skip coordinator state: {_var_var_7}")
            self.stats.skipped += 1
        return None if True else None

    async def _migrate_production_time_state(self, data: Dict[str, Any]) -> None:
        try:
            _var_var_92 = data.get("date")
            if _var_var_92:
                try:
                    _var_var_92 = datetime.fromisoformat(_var_var_92).date()
                except Exception:
                    _var_var_92 = datetime.now().date()
            else:
                _var_var_92 = datetime.now().date()
            _var_var_49 = data.get("last_updated")
            if _var_var_49:
                try:
                    _var_var_49 = datetime.fromisoformat(_var_var_49)
                except Exception:
                    _var_var_49 = datetime.now()
            else:
                _var_var_49 = datetime.now()
            _var_var_33 = await self.db.fetchone(
                "SELECT id FROM production_time_state WHERE id = 1"
            )
            if _var_var_33:
                await self.db.execute(
                    "UPDATE production_time_state SET date = ?, accumulated_hours = ?, is_active = ?, start_time = ?, production_time_today = ?, last_updated = ? WHERE id = 1",
                    (
                        _var_var_92,
                        data.get("accumulated_hours", 0),
                        data.get("is_active", False),
                        data.get("start_time"),
                        data.get("production_time_today"),
                        _var_var_49,
                    ),
                )
                self.stats.updated += 1
            else:
                await self.db.execute(
                    "INSERT INTO production_time_state (id, date, accumulated_hours, is_active, start_time, production_time_today, last_updated) VALUES (1, ?, ?, ?, ?, ?, ?)",
                    (
                        _var_var_92,
                        data.get("accumulated_hours", 0),
                        data.get("is_active", False),
                        data.get("start_time"),
                        data.get("production_time_today"),
                        _var_var_49,
                    ),
                )
                self.stats.imported += 1
        except Exception as _var_var_7:
            _LOGGER.debug(f"Skip production time state: {_var_var_7}")
            self.stats.skipped += 1
        return None if True else None

    async def _migrate_weather_expert_weights(self, data: Dict[str, Any]) -> None:
        _var_var_93 = data.get("weights", {})
        _var_var_89 = data.get("metadata", {})
        _var_var_49 = _var_var_89.get("last_updated")
        if _var_var_49:
            try:
                _var_var_49 = datetime.fromisoformat(_var_var_49)
            except Exception:
                _var_var_49 = datetime.now()
        else:
            _var_var_49 = datetime.now()
        for _var_var_94, _var_var_95 in _var_var_93.items():
            for _var_var_96, _var_var_97 in _var_var_95.items():
                try:
                    await self.db.execute(
                        "INSERT OR REPLACE INTO weather_expert_weights (cloud_type, expert_name, weight, last_updated) VALUES (?, ?, ?, ?)",
                        (_var_var_94, _var_var_96, _var_var_97, _var_var_49),
                    )
                    self.stats.imported += 1
                except Exception as _var_var_7:
                    _LOGGER.debug(f"Skip weather expert weight: {_var_var_7}")
                    self.stats.skipped += 1
                    continue
        _var_var_98 = data.get("snow_prediction_stats", {})
        if _var_var_98:
            try:
                _var_var_99 = _var_var_98.get("last_updated")
                if _var_var_99:
                    try:
                        _var_var_99 = datetime.fromisoformat(_var_var_99)
                    except Exception:
                        _var_var_99 = None
                _var_var_33 = await self.db.fetchone(
                    "SELECT id FROM weather_expert_snow_stats WHERE id = 1"
                )
                if _var_var_33:
                    await self.db.execute(
                        "UPDATE weather_expert_snow_stats SET total_predictions = ?, correct_predictions = ?, accuracy = ?, last_updated = ? WHERE id = 1",
                        (
                            _var_var_98.get("total_predictions", 0),
                            _var_var_98.get("correct_predictions", 0),
                            _var_var_98.get("accuracy", 0.0),
                            _var_var_99,
                        ),
                    )
                    self.stats.updated += 1
                else:
                    await self.db.execute(
                        "INSERT INTO weather_expert_snow_stats (id, total_predictions, correct_predictions, accuracy, last_updated) VALUES (1, ?, ?, ?, ?)",
                        (
                            _var_var_98.get("total_predictions", 0),
                            _var_var_98.get("correct_predictions", 0),
                            _var_var_98.get("accuracy", 0.0),
                            _var_var_99,
                        ),
                    )
                    self.stats.imported += 1
            except Exception as _var_var_7:
                _LOGGER.debug(f"Skip snow prediction stats: {_var_var_7}")
        return None if True else None

    async def _migrate_hourly_predictions(self, data: Dict[str, Any]) -> None:
        _local2 = data if isinstance(data, list) else data.get("predictions", [])
        _local3 = len(_local2)
        if _local3 > 0:
            _LOGGER.info(f"   → Migrating {_local3} hourly predictions...")
        _local4 = 0
        _var_var_115 = []

        async def execute_no_commit(sql, params):
            _var_var_115.append((sql, params))

        for _local6 in _local2:
            _var_var_115 = []
            _local7 = _local6.get("id") or _local6.get("prediction_id")
            if not _local7:
                continue
            try:
                _local8 = await self.db.fetchone(
                    "SELECT id FROM hourly_predictions WHERE prediction_id = ?",
                    (_local7,),
                )
                _local9 = _local8 is not None
                _local10 = _local6.get("target_datetime")
                if _local10 and isinstance(_local10, str):
                    _local10 = datetime.fromisoformat(_local10)
                _local11 = _local6.get("target_date")
                if _local11 and isinstance(_local11, str):
                    _local11 = datetime.fromisoformat(_local11).date()
                _local12 = _local6.get("prediction_created_at")
                if _local12 and isinstance(_local12, str):
                    _local12 = datetime.fromisoformat(_local12)
                _local13 = _local6.get("actual_measured_at")
                if _local13 and isinstance(_local13, str):
                    _local13 = datetime.fromisoformat(_local13)
                _local14 = _local6.get("flags", {})
                _local15 = _local6.get("quality", {})
                if not _local9:
                    await execute_no_commit(
                        "INSERT INTO hourly_predictions (prediction_id, prediction_created_at, prediction_created_hour, target_datetime, target_date, target_hour, target_day_of_week, target_day_of_year, target_month, target_season, prediction_kwh, prediction_kwh_uncapped, prediction_method, ml_contribution_percent, model_version, confidence, actual_kwh, actual_measured_at, accuracy_percent, error_kwh, error_percent, is_production_hour, is_peak_hour, is_outlier, has_weather_alert, has_sensor_data, sensor_data_complete, weather_forecast_updated, manual_override, inverter_clipped, has_panel_group_predictions, prediction_confidence, weather_forecast_age_hours, sensor_data_quality, data_completeness_percent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            _local7,
                            _local12,
                            _local6.get("prediction_created_hour"),
                            _local10,
                            _local11,
                            _local6.get("target_hour"),
                            _local6.get("target_day_of_week"),
                            _local6.get("target_day_of_year"),
                            _local6.get("target_month"),
                            _local6.get("target_season"),
                            _local6.get("prediction_kwh", 0),
                            _local6.get("prediction_kwh_uncapped"),
                            _local6.get("prediction_method"),
                            _local6.get("ml_contribution_percent"),
                            _local6.get("model_version"),
                            _local6.get("confidence"),
                            _local6.get("actual_kwh"),
                            _local13,
                            _local6.get("accuracy_percent"),
                            _local6.get("error_kwh"),
                            _local6.get("error_percent"),
                            _local14.get("is_production_hour", False),
                            _local14.get("is_peak_hour", False),
                            _local14.get("is_outlier", False),
                            _local14.get("has_weather_alert", False),
                            _local14.get("has_sensor_data", False),
                            _local14.get("sensor_data_complete", False),
                            _local14.get("weather_forecast_updated", False),
                            _local14.get("manual_override", False),
                            _local14.get("inverter_clipped", False),
                            _local14.get("has_panel_group_predictions", False),
                            _local15.get("prediction_confidence"),
                            _local15.get("weather_forecast_age_hours"),
                            _local15.get("sensor_data_quality"),
                            _local15.get("data_completeness_percent"),
                        ),
                    )
                    self.stats.imported += 1
                else:
                    _local16 = _local6.get("actual_kwh")
                    if _local16 is not None:
                        await execute_no_commit(
                            "UPDATE hourly_predictions SET\n                               actual_kwh = ?,\n                               actual_measured_at = ?,\n                               accuracy_percent = ?,\n                               error_kwh = ?,\n                               error_percent = ?\n                            WHERE prediction_id = ?\n                              AND (actual_kwh IS NULL OR actual_kwh = 0)",
                            (
                                _local16,
                                _local13,
                                _local6.get("accuracy_percent"),
                                _local6.get("error_kwh"),
                                _local6.get("error_percent"),
                                _local7,
                            ),
                        )
                        self.stats.updated += 1
                    else:
                        self.stats.skipped += 1
                _local17 = _local6.get("sensor_actual", {})
                if _local17 and isinstance(_local17, dict):
                    try:
                        await execute_no_commit(
                            "INSERT OR REPLACE INTO prediction_sensor_actual (prediction_id, temperature_c, humidity_percent, solar_radiation_wm2, rain_mm, uv_index, wind_speed_ms, current_yield_kwh, lux) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                _local7,
                                _local17.get("temperature_c"),
                                _local17.get("humidity_percent"),
                                _local17.get("solar_radiation_wm2"),
                                _local17.get("rain_mm"),
                                _local17.get("uv_index"),
                                _local17.get("wind_speed_ms"),
                                _local17.get("current_yield_kwh"),
                                _local17.get("lux"),
                            ),
                        )
                        self.stats.imported += 1
                    except Exception:
                        pass
                for _local18, _local19 in (
                    ("forecast", "weather_forecast"),
                    ("corrected", "weather_corrected"),
                    ("actual", "weather_actual"),
                ):
                    _local20 = _local6.get(_local19)
                    if _local20 and isinstance(_local20, dict):
                        try:
                            if _local18 == "actual":
                                _local21 = _local20.get("temperature_c")
                                _local22 = _local20.get("wind_speed_ms")
                                _local23 = _local20.get("humidity_percent")
                                _local24 = _local20.get("precipitation_mm")
                                _local25 = _local20.get("cloud_cover_percent")
                                _local26 = _local20.get("pressure_hpa")
                                _local27 = _local20.get("lux")
                                _local28 = _local20.get("frost_detected")
                                _local29 = _local20.get("frost_score")
                                _local30 = _local20.get("frost_confidence")
                            else:
                                _local21 = _local20.get("temperature")
                                _local22 = _local20.get("wind")
                                _local23 = _local20.get("humidity")
                                _local24 = _local20.get("rain")
                                _local25 = _local20.get("clouds")
                                _local26 = _local20.get("pressure")
                                _local27 = None
                                _local28 = None
                                _local29 = None
                                _local30 = None
                            await execute_no_commit(
                                "INSERT OR REPLACE INTO prediction_weather (prediction_id, weather_type, temperature, solar_radiation_wm2, wind, humidity, rain, clouds, pressure, source, lux, frost_detected, frost_score, frost_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    _local7,
                                    _local18,
                                    _sql_value(_local21),
                                    _sql_value(_local20.get("solar_radiation_wm2")),
                                    _sql_value(_local22),
                                    _sql_value(_local23),
                                    _sql_value(_local24),
                                    _sql_value(_local25),
                                    _sql_value(_local26),
                                    _sql_value(_local20.get("source")),
                                    _sql_value(_local27),
                                    _sql_value(_local28),
                                    _sql_value(_local29),
                                    _sql_value(_local30),
                                ),
                            )
                            self.stats.imported += 1
                        except Exception:
                            continue
                _local31 = _local6.get("astronomy")
                if _local31 and isinstance(_local31, dict):
                    try:
                        await execute_no_commit(
                            "INSERT OR REPLACE INTO prediction_astronomy (prediction_id, sunrise, sunset, solar_noon, daylight_hours, sun_elevation_deg, sun_azimuth_deg, clear_sky_radiation_wm2, theoretical_max_kwh, hours_since_solar_noon, day_progress_ratio, hours_after_sunrise, hours_before_sunset) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                _local7,
                                _sql_value(_local31.get("sunrise")),
                                _sql_value(_local31.get("sunset")),
                                _sql_value(_local31.get("solar_noon")),
                                _sql_value(_local31.get("daylight_hours")),
                                _sql_value(_local31.get("sun_elevation_deg")),
                                _sql_value(_local31.get("sun_azimuth_deg")),
                                _sql_value(_local31.get("clear_sky_radiation_wm2")),
                                _sql_value(_local31.get("theoretical_max_kwh")),
                                _sql_value(_local31.get("hours_since_solar_noon")),
                                _sql_value(_local31.get("day_progress_ratio")),
                                _sql_value(_local31.get("hours_after_sunrise")),
                                _sql_value(_local31.get("hours_before_sunset")),
                            ),
                        )
                        self.stats.imported += 1
                    except Exception:
                        pass
                _local32 = _local6.get("panel_group_predictions") or {}
                _local33 = _local6.get("panel_group_actuals") or {}
                _local34 = set(_local32.keys()) | set(_local33.keys())
                for _local35 in _local34:
                    _local36 = _local32.get(_local35)
                    _local37 = _local33.get(_local35)
                    if _local36 is not None or _local37 is not None:
                        try:
                            await execute_no_commit(
                                "INSERT OR REPLACE INTO prediction_panel_groups (prediction_id, group_name, prediction_kwh, actual_kwh) VALUES (?, ?, ?, ?)",
                                (_local7, _local35, _local36 or 0, _local37),
                            )
                            self.stats.imported += 1
                        except Exception:
                            continue
                _local38 = _local6.get("shadow_detection")
                if _local38 and isinstance(_local38, dict):
                    try:
                        _local39 = _local38.get("methods", {})
                        _local40 = _local39.get("theory_ratio", {})
                        _local41 = _local39.get("sensor_fusion", {})
                        _local42 = _local38.get("weights", {})
                        await execute_no_commit(
                            "INSERT OR REPLACE INTO hourly_shadow_detection (\n                                prediction_id, method, ensemble_mode, shadow_type, shadow_percent,\n                                confidence, root_cause, fusion_mode, efficiency_ratio, loss_kwh,\n                                theoretical_max_kwh, interpretation,\n                                theory_ratio_shadow_type, theory_ratio_shadow_percent, theory_ratio_confidence,\n                                theory_ratio_efficiency_ratio, theory_ratio_clear_sky_wm2, theory_ratio_actual_wm2,\n                                theory_ratio_loss_kwh, theory_ratio_root_cause,\n                                sensor_fusion_shadow_type, sensor_fusion_shadow_percent, sensor_fusion_confidence,\n                                sensor_fusion_efficiency_ratio, sensor_fusion_loss_kwh, sensor_fusion_root_cause,\n                                sensor_fusion_lux_factor, sensor_fusion_lux_shadow_percent,\n                                sensor_fusion_irradiance_factor, sensor_fusion_irradiance_shadow_percent,\n                                weight_theory_ratio, weight_sensor_fusion\n                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                _local7,
                                _sql_value(_local38.get("method")),
                                _sql_value(_local38.get("ensemble_mode")),
                                _sql_value(_local38.get("shadow_type")),
                                _sql_value(_local38.get("shadow_percent")),
                                _sql_value(_local38.get("confidence")),
                                _sql_value(_local38.get("root_cause")),
                                _sql_value(_local38.get("fusion_mode")),
                                _sql_value(_local38.get("efficiency_ratio")),
                                _sql_value(_local38.get("loss_kwh")),
                                _sql_value(_local38.get("theoretical_max_kwh")),
                                _sql_value(_local38.get("interpretation")),
                                _sql_value(_local40.get("shadow_type")),
                                _sql_value(_local40.get("shadow_percent")),
                                _sql_value(_local40.get("confidence")),
                                _sql_value(_local40.get("efficiency_ratio")),
                                _sql_value(_local40.get("clear_sky_wm2")),
                                _sql_value(_local40.get("actual_wm2")),
                                _sql_value(_local40.get("loss_kwh")),
                                _sql_value(_local40.get("root_cause")),
                                _sql_value(_local41.get("shadow_type")),
                                _sql_value(_local41.get("shadow_percent")),
                                _sql_value(_local41.get("confidence")),
                                _sql_value(_local41.get("efficiency_ratio")),
                                _sql_value(_local41.get("loss_kwh")),
                                _sql_value(_local41.get("root_cause")),
                                _sql_value(_local41.get("lux_factor")),
                                _sql_value(_local41.get("lux_shadow_percent")),
                                _sql_value(_local41.get("irradiance_factor")),
                                _sql_value(_local41.get("irradiance_shadow_percent")),
                                _sql_value(_local42.get("theory_ratio")),
                                _sql_value(_local42.get("sensor_fusion")),
                            ),
                        )
                        self.stats.imported += 1
                    except Exception as _local43:
                        _LOGGER.debug(f"Skip shadow_detection: {_local43}")
                _local44 = _local6.get("production_metrics")
                if _local44 and isinstance(_local44, dict):
                    try:
                        await execute_no_commit(
                            "INSERT OR REPLACE INTO hourly_production_metrics (\n                                prediction_id, peak_power_today_kwh, production_hours_today, cumulative_today_kwh\n                            ) VALUES (?, ?, ?, ?)",
                            (
                                _local7,
                                _sql_value(_local44.get("peak_power_today_kwh")),
                                _sql_value(_local44.get("production_hours_today")),
                                _sql_value(_local44.get("cumulative_today_kwh")),
                            ),
                        )
                        self.stats.imported += 1
                    except Exception as _local43:
                        _LOGGER.debug(f"Skip production_metrics: {_local43}")
                _local45 = _local6.get("historical_context")
                if _local45 and isinstance(_local45, dict):
                    try:
                        await execute_no_commit(
                            "INSERT OR REPLACE INTO hourly_historical_context (\n                                prediction_id, yesterday_same_hour, same_hour_avg_7days\n                            ) VALUES (?, ?, ?)",
                            (
                                _local7,
                                _sql_value(_local45.get("yesterday_same_hour")),
                                _sql_value(_local45.get("same_hour_avg_7days")),
                            ),
                        )
                        self.stats.imported += 1
                    except Exception as _local43:
                        _LOGGER.debug(f"Skip historical_context: {_local43}")
                _local46 = _local6.get("panel_group_accuracy")
                if _local46 and isinstance(_local46, dict):
                    for _local35, _local47 in _local46.items():
                        if _local47 and isinstance(_local47, dict):
                            try:
                                await execute_no_commit(
                                    "INSERT OR REPLACE INTO hourly_panel_group_accuracy (\n                                        prediction_id, group_name, prediction_kwh, actual_kwh,\n                                        error_kwh, error_percent, accuracy_percent\n                                    ) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (
                                        _local7,
                                        _local35,
                                        _sql_value(_local47.get("prediction_kwh")),
                                        _sql_value(_local47.get("actual_kwh")),
                                        _sql_value(_local47.get("error_kwh")),
                                        _sql_value(_local47.get("error_percent")),
                                        _sql_value(_local47.get("accuracy_percent")),
                                    ),
                                )
                                self.stats.imported += 1
                            except Exception as _local43:
                                _LOGGER.debug(
                                    f"Skip panel_group_accuracy for {_local35}: {_local43}"
                                )
                async with self.db.transaction():
                    for _local48, _local49 in _var_var_115:
                        await self.db.execute(_local48, _local49, auto_commit=False)
                _local4 += 1
                if _local3 > 500:
                    if _local4 % 100 == 0:
                        _local50 = _local4 / _local3 * 100
                        _LOGGER.info(
                            f"   → Progress: {_local4}/{_local3} ({_local50:.1f}%)"
                        )
            except Exception as _local43:
                _LOGGER.debug(f"Skip hourly prediction: {_local43}")
                self.stats.skipped += 1
                continue

    async def _migrate_daily_forecasts(self, data: Dict[str, Any]) -> None:
        if False:
            __assert_armored__ = None
        _var_var_48 = data.get("history", [])
        _var_var_160 = data.get("today", {})
        _var_var_161 = len(_var_var_48) + (1 if _var_var_160 else 0)
        if _var_var_161 > 0:
            _LOGGER.info(f"   → Migrating {_var_var_161} daily forecasts...")
        if _var_var_160 and _var_var_160.get("date"):
            try:
                _var_var_162 = _var_var_160.get("date")
                if isinstance(_var_var_162, str):
                    _var_var_162 = datetime.fromisoformat(_var_var_162).date()
                _var_var_163 = {
                    "forecast_day": "today",
                    "forecast_tomorrow": "tomorrow",
                    "forecast_day_after_tomorrow": "day_after_tomorrow",
                }
                for _var_var_164, _var_var_165 in _var_var_163.items():
                    _var_var_166 = _var_var_160.get(_var_var_164)
                    if _var_var_166 and isinstance(_var_var_166, dict):
                        _var_var_148 = _var_var_166.get("prediction_kwh")
                        _var_var_167 = _var_var_166.get(
                            "prediction_kwh_raw", _var_var_148
                        )
                        _var_var_168 = _var_var_166.get("safeguard_applied", False)
                        _var_var_169 = _var_var_166.get("safeguard_reduction_kwh")
                        _var_var_170 = _var_var_166.get("locked", False)
                        _var_var_171 = _var_var_166.get("locked_at")
                        _var_var_172 = _var_var_166.get("source")
                        _var_var_173 = _var_var_166.get("date")
                        if _var_var_173 and isinstance(_var_var_173, str):
                            _var_var_173 = datetime.fromisoformat(_var_var_173).date()
                        else:
                            _var_var_173 = _var_var_162
                        if _var_var_171 and isinstance(_var_var_171, str):
                            _var_var_171 = datetime.fromisoformat(_var_var_171)
                        if _var_var_148 is not None:
                            await self.db.execute(
                                "INSERT OR REPLACE INTO daily_forecasts (forecast_type, forecast_date, prediction_kwh, prediction_kwh_raw, safeguard_applied, safeguard_reduction_kwh, locked, locked_at, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    _var_var_165,
                                    _var_var_173,
                                    _var_var_148,
                                    _var_var_167,
                                    _var_var_168,
                                    _var_var_169,
                                    _var_var_170,
                                    _var_var_171,
                                    _var_var_172,
                                ),
                            )
                            self.stats.imported += 1
                        _var_var_174 = _var_var_166.get("updates", [])
                        for _var_var_175 in _var_var_174:
                            _var_var_176 = _var_var_175.get("prediction_kwh")
                            _var_var_177 = _var_var_175.get("updated_at")
                            _var_var_178 = _var_var_175.get("source")
                            if _var_var_177 and isinstance(_var_var_177, str):
                                _var_var_177 = datetime.fromisoformat(_var_var_177)
                            if _var_var_176 is not None:
                                await self.db.execute(
                                    "INSERT INTO daily_forecast_updates (forecast_type, forecast_date, prediction_kwh, source, updated_at) VALUES (?, ?, ?, ?, ?)",
                                    (
                                        _var_var_165,
                                        _var_var_173,
                                        _var_var_176,
                                        _var_var_178,
                                        _var_var_177,
                                    ),
                                )
                                self.stats.imported += 1
                        continue
            except Exception as _var_var_7:
                _LOGGER.debug(f"Skip today forecast: {_var_var_7}")
        for _var_var_75 in _var_var_48:
            _var_var_107 = _var_var_75.get("date")
            if not _var_var_107:
                continue
            try:
                _var_var_109 = (
                    datetime.fromisoformat(_var_var_107).date()
                    if isinstance(_var_var_107, str)
                    else _var_var_107
                )
                _var_var_179 = _var_var_75.get("predicted_kwh")
                _var_var_180 = _var_var_75.get("actual_kwh")
                _var_var_181 = _var_var_75.get("accuracy")
                _var_var_182 = _var_var_75.get("production_hours")
                _var_var_183 = _var_var_75.get("peak_power_w")
                _var_var_172 = _var_var_75.get("forecast_source")
                _var_var_33 = await self.db.fetchone(
                    "SELECT id FROM daily_summaries WHERE date = ?", (_var_var_109,)
                )
                if _var_var_33:
                    await self.db.execute(
                        "UPDATE daily_summaries SET predicted_total_kwh = COALESCE(predicted_total_kwh, ?), actual_total_kwh = COALESCE(actual_total_kwh, ?), accuracy_percent = COALESCE(accuracy_percent, ?), production_hours = COALESCE(production_hours, ?), peak_power_w = COALESCE(peak_power_w, ?) WHERE id = ?",
                        (
                            _var_var_179,
                            _var_var_180,
                            _var_var_181,
                            _var_var_182,
                            _var_var_183,
                            _var_var_33[0],
                        ),
                    )
                    self.stats.updated += 1
                    continue
                await self.db.execute(
                    "INSERT INTO daily_summaries (date, predicted_total_kwh, actual_total_kwh, accuracy_percent, production_hours, peak_power_w) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _var_var_109,
                        _var_var_179,
                        _var_var_180,
                        _var_var_181,
                        _var_var_182,
                        _var_var_183,
                    ),
                )
                self.stats.imported += 1
            except Exception as _var_var_7:
                _LOGGER.debug(f"Skip daily forecast history: {_var_var_7}")
                self.stats.skipped += 1
                continue
        _var_var_184 = data.get("date")
        if _var_var_184:
            try:
                if isinstance(_var_var_184, str):
                    _var_var_184 = datetime.fromisoformat(_var_var_184).date()
                _var_var_185 = data.get("forecast_best_hour", {})
                _var_var_186 = data.get("actual_best_hour", {})
                _var_var_187 = data.get("forecast_next_hour", {})
                _var_var_188 = data.get("production_time", {})
                _var_var_189 = data.get("peak_today", {})
                _var_var_190 = data.get("yield_today", {})
                _var_var_191 = data.get("consumption_today", {})
                _var_var_192 = data.get("autarky", {})
                _var_var_193 = data.get("finalized", {})
                _var_var_194 = _var_var_193.get("excluded_hours", {})
                _var_var_195 = _var_var_185.get("locked_at")
                if _var_var_195 and isinstance(_var_var_195, str):
                    _var_var_195 = datetime.fromisoformat(_var_var_195)
                _var_var_196 = _var_var_186.get("saved_at")
                if _var_var_196 and isinstance(_var_var_196, str):
                    _var_var_196 = datetime.fromisoformat(_var_var_196)
                _var_var_197 = _var_var_187.get("updated_at")
                if _var_var_197 and isinstance(_var_var_197, str):
                    _var_var_197 = datetime.fromisoformat(_var_var_197)
                _var_var_198 = _var_var_188.get("start_time")
                if _var_var_198 and isinstance(_var_var_198, str):
                    _var_var_198 = datetime.fromisoformat(_var_var_198)
                _var_var_199 = _var_var_188.get("end_time")
                if _var_var_199 and isinstance(_var_var_199, str):
                    _var_var_199 = datetime.fromisoformat(_var_var_199)
                _var_var_200 = _var_var_188.get("last_power_above_10w")
                if _var_var_200 and isinstance(_var_var_200, str):
                    _var_var_200 = datetime.fromisoformat(_var_var_200)
                _var_var_201 = _var_var_188.get("zero_power_since")
                if _var_var_201 and isinstance(_var_var_201, str):
                    _var_var_201 = datetime.fromisoformat(_var_var_201)
                _var_var_202 = _var_var_189.get("at")
                if _var_var_202 and isinstance(_var_var_202, str):
                    _var_var_202 = datetime.fromisoformat(_var_var_202)
                _var_var_203 = _var_var_192.get("calculated_at")
                if _var_var_203 and isinstance(_var_var_203, str):
                    _var_var_203 = datetime.fromisoformat(_var_var_203)
                _var_var_204 = _var_var_193.get("at")
                if _var_var_204 and isinstance(_var_var_204, str):
                    _var_var_204 = datetime.fromisoformat(_var_var_204)
                await self.db.execute(
                    "INSERT OR REPLACE INTO daily_forecast_tracking (\n                        date, forecast_best_hour, forecast_best_hour_kwh, forecast_best_hour_locked,\n                        forecast_best_hour_locked_at, forecast_best_hour_source,\n                        actual_best_hour, actual_best_hour_kwh, actual_best_hour_saved_at,\n                        forecast_next_hour_period, forecast_next_hour_kwh, forecast_next_hour_updated_at,\n                        forecast_next_hour_source, production_time_active, production_time_duration_seconds,\n                        production_time_start, production_time_end, production_time_last_power_above_10w,\n                        production_time_zero_power_since, peak_today_power_w, peak_today_at,\n                        yield_today_kwh, yield_today_sensor, consumption_today_kwh, consumption_today_sensor,\n                        autarky_percent, autarky_calculated_at, finalized_yield_kwh, finalized_consumption_kwh,\n                        finalized_production_hours, finalized_accuracy_percent, finalized_excluded_hours_count,\n                        finalized_excluded_hours_total, finalized_excluded_hours_ratio,\n                        finalized_excluded_hours_reasons, finalized_at\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _var_var_184,
                        _sql_value(_var_var_185.get("hour")),
                        _sql_value(_var_var_185.get("prediction_kwh")),
                        _sql_value(_var_var_185.get("locked")),
                        _var_var_195,
                        _sql_value(_var_var_185.get("source")),
                        _sql_value(_var_var_186.get("hour")),
                        _sql_value(_var_var_186.get("actual_kwh")),
                        _var_var_196,
                        _sql_value(_var_var_187.get("period")),
                        _sql_value(_var_var_187.get("prediction_kwh")),
                        _var_var_197,
                        _sql_value(_var_var_187.get("source")),
                        _sql_value(_var_var_188.get("active")),
                        _sql_value(_var_var_188.get("duration_seconds")),
                        _var_var_198,
                        _var_var_199,
                        _var_var_200,
                        _var_var_201,
                        _sql_value(_var_var_189.get("power_w")),
                        _var_var_202,
                        _sql_value(_var_var_190.get("kwh")),
                        _sql_value(_var_var_190.get("sensor")),
                        _sql_value(_var_var_191.get("kwh")),
                        _sql_value(_var_var_191.get("sensor")),
                        _sql_value(_var_var_192.get("percent")),
                        _var_var_203,
                        _sql_value(_var_var_193.get("yield_kwh")),
                        _sql_value(_var_var_193.get("consumption_kwh")),
                        _sql_value(_var_var_193.get("production_hours")),
                        _sql_value(_var_var_193.get("accuracy_percent")),
                        _sql_value(_var_var_194.get("count")),
                        _sql_value(_var_var_194.get("total")),
                        _sql_value(_var_var_194.get("ratio")),
                        _sql_value(_var_var_194.get("reasons")),
                        _var_var_204,
                    ),
                )
                self.stats.imported += 1
            except Exception as _var_var_7:
                _LOGGER.debug(f"Skip daily_forecast_tracking: {_var_var_7}")
        _var_var_205 = data.get("statistics", {})
        if _var_var_205:
            try:
                _var_var_206 = _var_var_205.get("all_time_peak", {})
                _var_var_207 = _var_var_205.get("current_week", {})
                _var_var_208 = _var_var_205.get("current_month", {})
                _var_var_209 = _var_var_205.get("last_7_days", {})
                _var_var_210 = _var_var_205.get("last_30_days", {})
                _var_var_211 = _var_var_205.get("last_365_days", {})
                _var_var_212 = _var_var_206.get("date")
                if _var_var_212 and isinstance(_var_var_212, str):
                    _var_var_212 = datetime.fromisoformat(_var_var_212).date()
                _var_var_213 = _var_var_206.get("at")
                if _var_var_213 and isinstance(_var_var_213, str):
                    _var_var_213 = datetime.fromisoformat(_var_var_213)
                _var_var_214 = _var_var_207.get("updated_at")
                if _var_var_214 and isinstance(_var_var_214, str):
                    _var_var_214 = datetime.fromisoformat(_var_var_214)
                _var_var_215 = _var_var_208.get("updated_at")
                if _var_var_215 and isinstance(_var_var_215, str):
                    _var_var_215 = datetime.fromisoformat(_var_var_215)
                _var_var_216 = _var_var_209.get("calculated_at")
                if _var_var_216 and isinstance(_var_var_216, str):
                    _var_var_216 = datetime.fromisoformat(_var_var_216)
                _var_var_217 = _var_var_210.get("calculated_at")
                if _var_var_217 and isinstance(_var_var_217, str):
                    _var_var_217 = datetime.fromisoformat(_var_var_217)
                _var_var_218 = _var_var_211.get("calculated_at")
                if _var_var_218 and isinstance(_var_var_218, str):
                    _var_var_218 = datetime.fromisoformat(_var_var_218)
                await self.db.execute(
                    "INSERT OR REPLACE INTO daily_statistics (\n                        id, all_time_peak_power_w, all_time_peak_date, all_time_peak_at,\n                        current_week_period, current_week_date_range, current_week_yield_kwh,\n                        current_week_consumption_kwh, current_week_days, current_week_updated_at,\n                        current_month_period, current_month_yield_kwh, current_month_consumption_kwh,\n                        current_month_avg_autarky, current_month_days, current_month_updated_at,\n                        last_7_days_avg_yield_kwh, last_7_days_avg_accuracy, last_7_days_total_yield_kwh, last_7_days_calculated_at,\n                        last_30_days_avg_yield_kwh, last_30_days_avg_accuracy, last_30_days_total_yield_kwh, last_30_days_calculated_at,\n                        last_365_days_avg_yield_kwh, last_365_days_total_yield_kwh, last_365_days_calculated_at\n                    ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _sql_value(_var_var_206.get("power_w")),
                        _var_var_212,
                        _var_var_213,
                        _sql_value(_var_var_207.get("period")),
                        _sql_value(_var_var_207.get("date_range")),
                        _sql_value(_var_var_207.get("yield_kwh")),
                        _sql_value(_var_var_207.get("consumption_kwh")),
                        _sql_value(_var_var_207.get("days")),
                        _var_var_214,
                        _sql_value(_var_var_208.get("period")),
                        _sql_value(_var_var_208.get("yield_kwh")),
                        _sql_value(_var_var_208.get("consumption_kwh")),
                        _sql_value(_var_var_208.get("avg_autarky")),
                        _sql_value(_var_var_208.get("days")),
                        _var_var_215,
                        _sql_value(_var_var_209.get("avg_yield_kwh")),
                        _sql_value(_var_var_209.get("avg_accuracy")),
                        _sql_value(_var_var_209.get("total_yield_kwh")),
                        _var_var_216,
                        _sql_value(_var_var_210.get("avg_yield_kwh")),
                        _sql_value(_var_var_210.get("avg_accuracy")),
                        _sql_value(_var_var_210.get("total_yield_kwh")),
                        _var_var_217,
                        _sql_value(_var_var_211.get("avg_yield_kwh")),
                        _sql_value(_var_var_211.get("total_yield_kwh")),
                        _var_var_218,
                    ),
                )
                self.stats.imported += 1
            except Exception as _var_var_7:
                _LOGGER.debug(f"Skip daily_statistics: {_var_var_7}")
        return None if True else None

    async def _migrate_daily_summaries(self, data: Dict[str, Any]) -> None:
        _var_var_219 = data if isinstance(data, list) else data.get("summaries", [])
        for _var_var_220 in _var_var_219:
            _var_var_92 = _var_var_220.get("date")
            if not _var_var_92:
                continue
            try:
                if isinstance(_var_var_92, str):
                    _var_var_92 = datetime.fromisoformat(_var_var_92).date()
                _var_var_221 = _var_var_220.get("overall", {})
                if not _var_var_221:
                    _var_var_221 = _var_var_220
                _var_var_33 = await self.db.fetchone(
                    "SELECT id FROM daily_summaries WHERE date = ?", (_var_var_92,)
                )
                _var_var_179 = _var_var_221.get(
                    "predicted_total_kwh"
                ) or _var_var_221.get("predicted_kwh")
                _var_var_180 = _var_var_221.get("actual_total_kwh") or _var_var_221.get(
                    "actual_kwh"
                )
                _var_var_181 = _var_var_221.get("accuracy_percent") or _var_var_221.get(
                    "accuracy"
                )
                _var_var_222 = _var_var_221.get("error_kwh") or _var_var_221.get(
                    "absolute_error_kwh"
                )
                _var_var_182 = _var_var_221.get("production_hours")
                _var_var_183 = _var_var_221.get("peak_power_w")
                _var_var_223 = _var_var_221.get("peak_hour")
                if _var_var_33:
                    await self.db.execute(
                        "UPDATE daily_summaries SET predicted_total_kwh = ?, actual_total_kwh = ?, accuracy_percent = ?, error_kwh = ?, production_hours = ?, peak_power_w = ?, peak_hour = ? WHERE id = ?",
                        (
                            _var_var_179,
                            _var_var_180,
                            _var_var_181,
                            _var_var_222,
                            _var_var_182,
                            _var_var_183,
                            _var_var_223,
                            _var_var_33[0],
                        ),
                    )
                    self.stats.updated += 1
                else:
                    await self.db.execute(
                        "INSERT INTO daily_summaries (date, predicted_total_kwh, actual_total_kwh, accuracy_percent, error_kwh, production_hours, peak_power_w, peak_hour, month, season) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            _var_var_92,
                            _var_var_179,
                            _var_var_180,
                            _var_var_181,
                            _var_var_222,
                            _var_var_182,
                            _var_var_183,
                            _var_var_223,
                            _var_var_220.get("month"),
                            _var_var_220.get("season"),
                        ),
                    )
                    self.stats.imported += 1
                _var_var_224 = _var_var_220.get("time_windows", {})
                if _var_var_224 and isinstance(_var_var_224, dict):
                    for _var_var_225, _var_var_226 in _var_var_224.items():
                        if not isinstance(_var_var_226, dict):
                            continue
                        try:
                            await self.db.execute(
                                "INSERT OR REPLACE INTO daily_summary_time_windows (date, window_name, predicted_kwh, actual_kwh, accuracy, stable, hours_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (
                                    _var_var_92,
                                    _var_var_225,
                                    _var_var_226.get("predicted_kwh"),
                                    _var_var_226.get("actual_kwh"),
                                    _var_var_226.get("accuracy"),
                                    _var_var_226.get("stable"),
                                    _var_var_226.get("hours_count"),
                                ),
                            )
                            self.stats.imported += 1
                        except Exception:
                            pass
                _var_var_227 = _var_var_220.get("frost_analysis", {})
                if _var_var_227:
                    if isinstance(_var_var_227, dict):
                        try:
                            await self.db.execute(
                                "INSERT OR REPLACE INTO daily_summary_frost_analysis (date, hours_analyzed, frost_detected, total_affected_hours, heavy_frost_hours, light_frost_hours) VALUES (?, ?, ?, ?, ?, ?)",
                                (
                                    _var_var_92,
                                    _var_var_227.get("hours_analyzed"),
                                    _var_var_227.get("frost_detected"),
                                    _var_var_227.get("total_affected_hours"),
                                    _var_var_227.get("heavy_frost_hours"),
                                    _var_var_227.get("light_frost_hours"),
                                ),
                            )
                            self.stats.imported += 1
                        except Exception:
                            pass
            except Exception as _var_var_7:
                _LOGGER.debug(f"Skip daily summary: {_var_var_7}")
                self.stats.skipped += 1

    async def _migrate_visibility_learning(self, data: Dict[str, Any]) -> None:
        try:
            _var_var_205 = data.get("stats", {})
            _var_var_93 = data.get("weights", {})
            _var_var_102 = _var_var_205.get("last_learning_date")
            if _var_var_102 and isinstance(_var_var_102, str):
                try:
                    _var_var_102 = datetime.fromisoformat(_var_var_102).date()
                except Exception:
                    _var_var_102 = None
            _var_var_49 = data.get("updated_at")
            if _var_var_49 and isinstance(_var_var_49, str):
                try:
                    _var_var_49 = datetime.fromisoformat(_var_var_49)
                except Exception:
                    _var_var_49 = datetime.now()
            else:
                _var_var_49 = datetime.now()
            _var_var_244 = _var_var_93.get("fog", {})
            _var_var_245 = _var_var_93.get("fog_light", {})
            await self.db.execute(
                "INSERT OR REPLACE INTO visibility_learning (id, version, has_solar_radiation_sensor, last_learning_date, total_fog_hours_learned, total_fog_light_hours_learned, bright_sky_fog_hits, pirate_weather_fog_hits, learning_sessions, fog_bright_sky_weight, fog_pirate_weather_weight, fog_light_bright_sky_weight, fog_light_pirate_weather_weight, last_updated) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    data.get("version", "1.0"),
                    data.get("has_solar_radiation_sensor", False),
                    _var_var_102,
                    _var_var_205.get("total_fog_hours_learned", 0),
                    _var_var_205.get("total_fog_light_hours_learned", 0),
                    _var_var_205.get("bright_sky_fog_hits", 0),
                    _var_var_205.get("pirate_weather_fog_hits", 0),
                    _var_var_205.get("learning_sessions", 0),
                    _var_var_244.get("bright_sky", 0.5),
                    _var_var_244.get("pirate_weather", 0.5),
                    _var_var_245.get("bright_sky", 0.5),
                    _var_var_245.get("pirate_weather", 0.5),
                    _var_var_49,
                ),
            )
            self.stats.imported += 1
        except Exception as _var_var_7:
            _LOGGER.debug(f"Skip visibility learning: {_var_var_7}")
            self.stats.skipped += 1
        return None if True else None

    async def _migrate_retrospective_forecast(self, data: Dict[str, Any]) -> None:
        try:
            _var_var_275 = data.get("forecast_summary", {})
            _var_var_276 = data.get("simulation_context", {})
            _var_var_277 = data.get("generated_at")
            if _var_var_277 and isinstance(_var_var_277, str):
                _var_var_277 = datetime.fromisoformat(_var_var_277)
            _var_var_124 = _var_var_276.get("target_date")
            if _var_var_124 and isinstance(_var_var_124, str):
                try:
                    _var_var_124 = datetime.fromisoformat(_var_var_124).date()
                except Exception:
                    _var_var_124 = None
            await self.db.execute(
                "INSERT OR REPLACE INTO retrospective_forecast (id, version, generated_at, simulated_forecast_time, sunrise_today, target_date, today_kwh, today_kwh_raw, safeguard_applied, tomorrow_kwh, day_after_tomorrow_kwh, method, confidence, best_hour, best_hour_kwh) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _sql_value(data.get("version", "1.0")),
                    _sql_value(_var_var_277),
                    _sql_value(_var_var_276.get("simulated_forecast_time")),
                    _sql_value(_var_var_276.get("sunrise_today")),
                    _var_var_124,
                    _sql_value(_var_var_275.get("today_kwh")),
                    _sql_value(_var_var_275.get("today_kwh_raw")),
                    _sql_value(_var_var_275.get("safeguard_applied", False)),
                    _sql_value(_var_var_275.get("tomorrow_kwh")),
                    _sql_value(_var_var_275.get("day_after_tomorrow_kwh")),
                    _sql_value(_var_var_275.get("method")),
                    _sql_value(_var_var_275.get("confidence")),
                    _sql_value(_var_var_275.get("best_hour")),
                    _sql_value(_var_var_275.get("best_hour_kwh")),
                ),
            )
            self.stats.imported += 1
            await self.db.execute("DELETE FROM retrospective_forecast_hourly")
            _var_var_255 = []
            for _var_var_110 in data.get("hourly_predictions", []):
                _var_var_53 = _var_var_110.get("hour")
                if _var_var_53 is not None:
                    _var_var_278 = _var_var_110.get("weather", {})
                    _var_var_144 = _var_var_110.get("astronomy", {})
                    _var_var_255.append(
                        (
                            _var_var_53,
                            _sql_value(_var_var_110.get("prediction_kwh")),
                            _sql_value(_var_var_278.get("temperature_c")),
                            _sql_value(_var_var_278.get("cloud_cover_percent")),
                            _sql_value(_var_var_278.get("humidity_percent")),
                            _sql_value(_var_var_278.get("wind_speed_ms")),
                            _sql_value(_var_var_278.get("precipitation_mm")),
                            _sql_value(_var_var_278.get("direct_radiation")),
                            _sql_value(_var_var_278.get("diffuse_radiation")),
                            _sql_value(_var_var_278.get("visibility_m")),
                            _sql_value(_var_var_278.get("fog_detected")),
                            _sql_value(_var_var_278.get("fog_type")),
                            _sql_value(_var_var_144.get("sun_elevation_deg")),
                            _sql_value(_var_var_144.get("sun_azimuth_deg")),
                            _sql_value(_var_var_144.get("theoretical_max_kwh")),
                            _sql_value(_var_var_144.get("clear_sky_radiation_wm2")),
                        )
                    )
            if _var_var_255:
                _var_var_114 = await self.db.executemany(
                    "INSERT OR REPLACE INTO retrospective_forecast_hourly (hour, prediction_kwh, temperature_c, cloud_cover_percent, humidity_percent, wind_speed_ms, precipitation_mm, direct_radiation, diffuse_radiation, visibility_m, fog_detected, fog_type, sun_elevation_deg, sun_azimuth_deg, theoretical_max_kwh, clear_sky_radiation_wm2) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _var_var_255,
                )
                self.stats.imported += _var_var_114
        except Exception as _var_var_7:
            _LOGGER.debug(f"Skip retrospective forecast: {_var_var_7}")
            self.stats.skipped += 1
        return None if True else None
