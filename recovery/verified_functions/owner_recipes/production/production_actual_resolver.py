# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..core.core_panel_group_forecast import get_group_power_wp


class ActualSourceResolver:

    async def resolve_from_recorder_power(
        self,
        *,
        entity_id: str,
        hour_start: datetime,
        hour_end: datetime,
        sensor_data: Dict[str, Any],
        source: str = "power_recorder",
        max_power_w: Optional[float] = None,
    ) -> ActualResolution:
        _var_var_3 = await self._load_recorder_power_events(
            entity_id=entity_id,
            start_time=hour_start,
            end_time=hour_end,
            max_power_w=max_power_w,
        )
        if not _var_var_3:
            return self._unavailable(source, "no_recorder_power_events")
        _var_var_4 = self._integrate_power_events(
            events=_var_var_3,
            hour_start=hour_start,
            hour_end=hour_end,
            max_power_w=max_power_w,
        )
        if _var_var_4["hour_kwh"] is None:
            return ActualResolution(
                source=source,
                hour_kwh=None,
                panel_group_actuals=None,
                sensor_data_updates={},
                quality="unavailable",
                total_power_coverage_percent=_var_var_4["coverage_percent"],
                group_power_coverage_percent=None,
                max_sample_gap_seconds=_var_var_4["max_sample_gap_seconds"],
                invalid_sample_count=_var_var_4["invalid_sample_count"],
                exclusion_reason=_var_var_4["exclusion_reason"],
                diagnostics=_var_var_4["diagnostics"],
            )
        _var_var_5 = "exact"
        _var_var_6 = None
        if (
            _var_var_4["coverage_percent"] < MIN_POWER_COVERAGE_PERCENT
            or (_var_var_4["max_uncovered_gap_seconds"] or 0)
            > MAX_POWER_UNCOVERED_GAP_SECONDS
        ):
            _var_var_5 = "partial"
            _var_var_6 = "power_coverage_partial"
        return ActualResolution(
            source=source,
            hour_kwh=round(_var_var_4["hour_kwh"], 4),
            panel_group_actuals=None,
            sensor_data_updates={},
            quality=_var_var_5,
            total_power_coverage_percent=_var_var_4["coverage_percent"],
            group_power_coverage_percent=None,
            max_sample_gap_seconds=_var_var_4["max_sample_gap_seconds"],
            invalid_sample_count=_var_var_4["invalid_sample_count"],
            exclusion_reason=_var_var_6,
            diagnostics=_var_var_4["diagnostics"],
        )
