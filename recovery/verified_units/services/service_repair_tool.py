# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Controlled live database repair operations for Solar Forecast ML."""

from __future__ import annotations
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from homeassistant.core import HomeAssistant, ServiceCall
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..data.data_ensemble_shadow import EnsembleShadowHandler

if TYPE_CHECKING:
    from ..coordinator import SolarForecastMLCoordinator
_LOGGER = logging.getLogger(__name__)
OPERATION_DAILY_CUMULATIVE_ACTUAL = "daily_cumulative_actual"
DEFAULT_DISTRIBUTION = "forecast_shape"
DEFAULT_GROUP_DISTRIBUTION = "historical_clean_share"
DEFAULT_LEARNING_POLICY = "exclude"


@dataclass
class RepairHour:
    """Single hourly repair candidate."""

    prediction_id: str
    hour: int
    prediction_kwh: float
    repaired_kwh: float
    cumulative_kwh: float


class RepairToolService:
    """Developer-only repair tool for controlled SFML database writes."""

    def __init__(
        self, hass: HomeAssistant, coordinator: "SolarForecastMLCoordinator"
    ) -> None:
        """Initialize the repair tool."""
        self.hass = hass
        self.coordinator = coordinator

    @property
    def db_manager(self):
        """Return the active database manager."""
        data_manager = getattr(self.coordinator, "data_manager", None)
        if not data_manager:
            return
        return getattr(data_manager, "_db_manager", None)

    async def handle_repair_tool(self, call: ServiceCall) -> None:
        """Handle the generic repair_tool Home Assistant service."""
        operation = call.data.get("operation")
        if operation != OPERATION_DAILY_CUMULATIVE_ACTUAL:
            _LOGGER.error("SFML repair_tool: unsupported operation=%s", operation)
            return
        try:
            result = await self._repair_daily_cumulative_actual(call.data)
            self._log_result(result)
        except Exception as error:
            _LOGGER.error("SFML repair_tool failed: %s", error, exc_info=True)
        # Preserve the verified shared-return edge.
        return None if True else None

    async def _repair_daily_cumulative_actual(
        self, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Repair completed hourly actuals from a manufacturer cumulative day value."""
        db = self.db_manager
        if db is None:
            raise RuntimeError("database manager unavailable")
        dry_run = bool(data.get("dry_run", True))
        acknowledged = bool(data.get("acknowledge_live_db_risk", False))
        if not dry_run and (not acknowledged):
            raise ValueError(
                "acknowledge_live_db_risk must be true when dry_run is false"
            )
        target_date = self._parse_date(data.get("date"))
        cumulative_kwh = self._parse_positive_float(
            data.get("cumulative_kwh"), "cumulative_kwh"
        )
        measured_at = self._parse_measured_at(data.get("measured_at"))
        distribution = data.get("distribution", DEFAULT_DISTRIBUTION)
        group_distribution = data.get("group_distribution", DEFAULT_GROUP_DISTRIBUTION)
        learning_policy = data.get("learning_policy", DEFAULT_LEARNING_POLICY)
        start_hour = self._parse_optional_hour(data.get("start_hour"), default=0)
        end_hour = self._parse_optional_hour(
            data.get("end_hour"),
            default=self._last_completed_hour(measured_at, target_date),
        )
        if distribution != DEFAULT_DISTRIBUTION:
            raise ValueError("only distribution='forecast_shape' is supported")
        if group_distribution != DEFAULT_GROUP_DISTRIBUTION:
            raise ValueError(
                "only group_distribution='historical_clean_share' is supported"
            )
        if learning_policy != DEFAULT_LEARNING_POLICY:
            raise ValueError("only learning_policy='exclude' is supported")
        if end_hour < start_hour:
            raise ValueError("end_hour must be greater than or equal to start_hour")
        rows = await db.fetchall(
            "SELECT prediction_id, target_hour, prediction_kwh, actual_kwh,\n                      manual_override, has_sensor_data, sensor_data_complete\n               FROM hourly_predictions\n               WHERE target_date = ?\n                 AND target_hour BETWEEN ? AND ?\n               ORDER BY target_hour",
            (target_date, start_hour, end_hour),
        )
        if not rows:
            raise ValueError(
                f"no hourly predictions found for {target_date} {start_hour}..{end_hour}"
            )
        if hasattr(db, "is_actual_backfill_blocked"):
            blocked_hours = []
            for hour in range(start_hour, end_hour + 1):
                if await db.is_actual_backfill_blocked(target_date, hour):
                    blocked_hours.append(hour)
            if blocked_hours:
                raise ValueError(
                    f"repair_tool refused to backfill emergency-quarantined hours: {target_date} hours={blocked_hours}"
                )
        existing_total_kwh = await self._existing_total(target_date, end_hour)
        repair_delta_kwh = round(cumulative_kwh - existing_total_kwh, 6)
        if repair_delta_kwh <= 0.0005:
            return {
                "operation": OPERATION_DAILY_CUMULATIVE_ACTUAL,
                "dry_run": dry_run,
                "target_date": target_date,
                "cumulative_kwh": cumulative_kwh,
                "existing_total_kwh": existing_total_kwh,
                "repair_delta_kwh": repair_delta_kwh,
                "repaired_hours": [],
                "message": "nothing to repair",
            }
        candidates = self._select_candidate_rows(rows)
        if not candidates:
            raise ValueError(
                "no eligible repair hours found; use start_hour/end_hour to narrow the intended repair window"
            )
        repair_hours = self._allocate_hourly_delta(candidates, repair_delta_kwh)
        group_allocations = await self._allocate_groups(target_date, repair_hours)
        cumulative_by_hour = await self._calculate_cumulative_by_hour(
            target_date,
            end_hour,
            {hour.hour: hour.repaired_kwh for hour in repair_hours},
        )
        for hour in repair_hours:
            hour.cumulative_kwh = cumulative_by_hour[hour.hour]
        result = {
            "operation": OPERATION_DAILY_CUMULATIVE_ACTUAL,
            "dry_run": dry_run,
            "target_date": target_date,
            "measured_at": measured_at.isoformat(),
            "cumulative_kwh": cumulative_kwh,
            "existing_total_kwh": existing_total_kwh,
            "repair_delta_kwh": repair_delta_kwh,
            "start_hour": start_hour,
            "end_hour": end_hour,
            "distribution": distribution,
            "group_distribution": group_distribution,
            "learning_policy": learning_policy,
            "live_cache_updated": False,
            "repaired_hours": [
                {
                    "prediction_id": hour.prediction_id,
                    "hour": hour.hour,
                    "actual_kwh": hour.repaired_kwh,
                    "cumulative_kwh": hour.cumulative_kwh,
                    "groups": group_allocations.get(hour.prediction_id, {}),
                }
                for hour in repair_hours
            ],
        }
        if dry_run:
            return result
        await self._apply_daily_cumulative_repair(
            measured_at=measured_at,
            repair_hours=repair_hours,
            group_allocations=group_allocations,
            audit_payload=result,
        )
        shadow_handler = EnsembleShadowHandler(self.db_manager)
        for hour in repair_hours:
            await shadow_handler.evaluate_prediction(hour.prediction_id)
        result["written"] = True
        return result

    def _parse_date(self, value: Any) -> str:
        if not value:
            return dt_util.now().date().isoformat()
        parsed_time = datetime.fromisoformat(str(value))
        return parsed_time.date().isoformat()

    def _parse_measured_at(self, value: Any) -> datetime:
        if not value:
            return dt_util.now()
        parsed_time = datetime.fromisoformat(str(value))
        if parsed_time.tzinfo is None:
            return dt_util.ensure_local(parsed_time)
        return parsed_time

    def _parse_positive_float(self, value: Any, field_name: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field_name} must be a positive number") from error
        if number <= 0:
            raise ValueError(f"{field_name} must be greater than zero")
        return round(number, 6)

    def _parse_optional_hour(self, value: Any, default: int) -> int:
        if value is None:
            return default
        hour = int(value)
        if hour < 0 or hour > 23:
            raise ValueError("hour must be between 0 and 23")
        return hour

    def _last_completed_hour(self, measured_at: datetime, target_date: str) -> int:
        if measured_at.date().isoformat() != target_date:
            return 23
        return max(0, measured_at.hour - 1)

    async def _existing_total(self, target_date: str, end_hour: int) -> float:
        row = await self.db_manager.fetchone(
            "SELECT COALESCE(SUM(actual_kwh), 0.0)\n               FROM hourly_predictions\n               WHERE target_date = ?\n                 AND target_hour <= ?\n                 AND actual_kwh IS NOT NULL",
            (target_date, end_hour),
        )
        return round(float(row[0] or 0.0), 6)

    def _select_candidate_rows(self, rows: list[Any]) -> list[Any]:
        candidates = []
        for row in rows:
            actual_kwh = row["actual_kwh"]
            prediction_kwh = float(row["prediction_kwh"] or 0.0)
            manual_override = bool(row["manual_override"])
            has_sensor_data = bool(row["has_sensor_data"])
            sensor_data_complete = bool(row["sensor_data_complete"])
            missing_actual = actual_kwh is None and prediction_kwh > 0.01
            eligible_zero = (
                not manual_override
                and (not has_sensor_data)
                and (not sensor_data_complete)
                and (float(actual_kwh or 0.0) == 0.0)
                and (prediction_kwh > 0.01)
            )
            if missing_actual or eligible_zero:
                candidates.append(row)
        return candidates

    def _allocate_hourly_delta(
        self, rows: list[Any], repair_delta: float
    ) -> list[RepairHour]:
        weights = [max(float(row["prediction_kwh"] or 0.0), 0.0) for row in rows]
        total_weight = sum(weights)
        if total_weight <= 0:
            weights = [1.0 for unused_row in rows]
            total_weight = float(len(rows))
        repairs = []
        remaining_kwh = round(repair_delta, 4)
        for row_index, row in enumerate(rows):
            if row_index == len(rows) - 1:
                allocated_kwh = max(0.0, remaining_kwh)
            else:
                allocated_kwh = round(
                    repair_delta * (weights[row_index] / total_weight), 4
                )
                remaining_kwh = round(remaining_kwh - allocated_kwh, 4)
            repairs.append(
                RepairHour(
                    prediction_id=row["prediction_id"],
                    hour=int(row["target_hour"]),
                    prediction_kwh=float(row["prediction_kwh"] or 0.0),
                    repaired_kwh=allocated_kwh,
                    cumulative_kwh=0.0,
                )
            )
        return repairs

    async def _allocate_groups(
        self, target_date: str, repair_hours: list[RepairHour]
    ) -> dict[str, dict[str, float]]:
        if not repair_hours:
            return {}
        historical_weights = await self._historical_group_weights(target_date)
        allocations = {}
        for hour in repair_hours:
            rows = await self.db_manager.fetchall(
                "SELECT group_name, prediction_kwh\n                   FROM prediction_panel_groups\n                   WHERE prediction_id = ?\n                   ORDER BY group_name",
                (hour.prediction_id,),
            )
            if not rows:
                allocations[hour.prediction_id] = {}
                continue
            weights = {
                row["group_name"]: historical_weights.get(
                    row["group_name"], max(float(row["prediction_kwh"] or 0.0), 0.0)
                )
                for row in rows
            }
            total_weight = sum(weights.values())
            if total_weight <= 0:
                weights = {row["group_name"]: 1.0 for row in rows}
                total_weight = float(len(rows))
            remaining_kwh = round(hour.repaired_kwh, 4)
            group_allocations = {}
            group_names = list(weights.keys())
            for row_index, group_name in enumerate(group_names):
                if row_index == len(group_names) - 1:
                    group_kwh = max(0.0, remaining_kwh)
                else:
                    group_kwh = round(
                        hour.repaired_kwh * (weights[group_name] / total_weight), 4
                    )
                    remaining_kwh = round(remaining_kwh - group_kwh, 4)
                group_allocations[group_name] = group_kwh
            allocations[hour.prediction_id] = group_allocations
            continue
        return allocations

    async def _historical_group_weights(self, target_date: str) -> dict[str, float]:
        rows = await self.db_manager.fetchall(
            "SELECT ppg.group_name, SUM(ppg.actual_kwh) AS actual_sum\n               FROM prediction_panel_groups ppg\n               JOIN hourly_predictions hp ON hp.prediction_id = ppg.prediction_id\n               WHERE hp.target_date < ?\n                 AND hp.target_date >= date(?, '-14 days')\n                 AND hp.actual_kwh IS NOT NULL\n                 AND COALESCE(hp.exclude_from_learning, 0) = 0\n                 AND ppg.actual_kwh IS NOT NULL\n                 AND COALESCE(ppg.exclude_from_learning_group, 0) = 0\n               GROUP BY ppg.group_name",
            (target_date, target_date),
        )
        weights = {
            row["group_name"]: max(float(row["actual_sum"] or 0.0), 0.0) for row in rows
        }
        return {
            group_name: group_kwh
            for group_name, group_kwh in weights.items()
            if group_kwh > 0
        }

    async def _calculate_cumulative_by_hour(
        self, target_date: str, end_hour: int, repairs_by_hour: dict[int, float]
    ) -> dict[int, float]:
        rows = await self.db_manager.fetchall(
            "SELECT target_hour, actual_kwh\n               FROM hourly_predictions\n               WHERE target_date = ?\n                 AND target_hour <= ?\n               ORDER BY target_hour",
            (target_date, end_hour),
        )
        running_total_kwh = 0.0
        cumulative_by_hour = {}
        for row in rows:
            hour = int(row["target_hour"])
            energy_kwh = repairs_by_hour.get(hour)
            if energy_kwh is None:
                energy_kwh = float(row["actual_kwh"] or 0.0)
            running_total_kwh = round(running_total_kwh + energy_kwh, 4)
            if hour in repairs_by_hour:
                cumulative_by_hour[hour] = running_total_kwh
        return cumulative_by_hour

    async def _apply_daily_cumulative_repair(
        self,
        measured_at: datetime,
        repair_hours: list[RepairHour],
        group_allocations: dict[str, dict[str, float]],
        audit_payload: dict[str, Any],
    ) -> None:
        db = self.db_manager
        async with db.transaction():
            for hour in repair_hours:
                await db.execute(
                    "UPDATE hourly_predictions\n                       SET actual_kwh = ?,\n                           actual_measured_at = ?,\n                           manual_override = 1,\n                           exclude_from_learning = 1,\n                           exclude_from_clean_evaluation = 1,\n                           has_sensor_data = 1,\n                           sensor_data_complete = 0,\n                           has_panel_group_actuals = ?,\n                           error_kwh = ROUND(? - prediction_kwh, 4),\n                           error_percent = CASE\n                               WHEN prediction_kwh > 0\n                               THEN ROUND(((? - prediction_kwh) / prediction_kwh) * 100.0, 2)\n                               ELSE NULL\n                           END,\n                           accuracy_percent = CASE\n                               WHEN prediction_kwh > 0\n                               THEN MAX(0.0, ROUND(100.0 - ABS(((? - prediction_kwh) / prediction_kwh) * 100.0), 2))\n                               ELSE NULL\n                           END\n                       WHERE prediction_id = ?",
                    (
                        hour.repaired_kwh,
                        measured_at.isoformat(),
                        1 if group_allocations.get(hour.prediction_id) else 0,
                        hour.repaired_kwh,
                        hour.repaired_kwh,
                        hour.repaired_kwh,
                        hour.prediction_id,
                    ),
                    auto_commit=False,
                )
                for group_name, actual_kwh in group_allocations.get(
                    hour.prediction_id, {}
                ).items():
                    await db.execute(
                        "UPDATE prediction_panel_groups\n                           SET actual_kwh = ?,\n                               exclude_from_learning_group = 1,\n                               exclusion_reason_group = 'manual_actual_repair'\n                           WHERE prediction_id = ?\n                             AND group_name = ?",
                        (actual_kwh, hour.prediction_id, group_name),
                        auto_commit=False,
                    )
            await db.execute(
                "INSERT INTO repair_tool_audit\n                   (created_at, operation, dry_run, payload_json)\n                   VALUES (?, ?, 0, ?)",
                (
                    dt_util.now().isoformat(),
                    audit_payload["operation"],
                    json.dumps(audit_payload, sort_keys=True),
                ),
                auto_commit=False,
            )
        if hasattr(self.coordinator, "_refresh_hourly_predictions_cache"):
            await self.coordinator._refresh_hourly_predictions_cache()
            self.coordinator.async_update_listeners()
        # Preserve the verified shared-return edge.
        return None if True else None

    def _log_result(self, result: dict[str, Any]) -> None:
        repaired_hours = result.get("repaired_hours", [])
        log_mode = "DRY RUN" if result.get("dry_run") else "WRITTEN"
        _LOGGER.warning(
            "SFML repair_tool %s operation=%s date=%s cumulative=%.4f delta=%.4f hours=%s live_cache_updated=%s",
            log_mode,
            result.get("operation"),
            result.get("target_date"),
            float(result.get("cumulative_kwh", 0.0)),
            float(result.get("repair_delta_kwh", 0.0)),
            [hour.get("hour") for hour in repaired_hours],
            result.get("live_cache_updated", False),
        )
        _LOGGER.info("SFML repair_tool result: %s", json.dumps(result, sort_keys=True))
