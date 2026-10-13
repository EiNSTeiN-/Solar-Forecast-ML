# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Engineering diagnostics report generator for Warp Core Simulation.
Generates monthly warp core performance reports in Markdown format.
Uses TelemetryManager for all containment telemetry operations.
"""
from __future__ import annotations
import asyncio
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from ..data.db_manager import DatabaseManager

_LOGGER = logging.getLogger(__name__)
SEASONS = {
    "winter": [12, 1, 2],
    "spring": [3, 4, 5],
    "summer": [6, 7, 8],
    "autumn": [9, 10, 11],
}


class SystemReportGenerator:
    """Generates monthly system reports in Markdown format. @zara"""

    def __init__(self, data_dir: Path, db_manager: Optional[DatabaseManager] = None):
        """Initialize the report generator. @zara"""
        self.data_dir = data_dir
        self.docs_dir = data_dir / "docs"
        self.report_file = self.docs_dir / "system_report.md"
        self._db = db_manager

    def set_db_manager(self, db_manager: DatabaseManager) -> None:
        """Set database manager after initialization. @zara"""
        self._db = db_manager

    async def generate_report(self) -> bool:
        """Generate the system report. @zara"""
        try:
            _LOGGER.info("Generating monthly system report...")
            geometry = await self._load_geometry_data()
            statistics = await self._load_statistics()
            history = await self._load_history()
            physics_telemetry = await self._load_physics_telemetry()
            report_content = self._build_report(
                geometry, statistics, history, physics_telemetry
            )
            await self._write_report(report_content)
            _LOGGER.info(f"System report generated: {self.report_file}")
        except Exception as error:
            _LOGGER.error(f"Failed to generate system report: {error}", exc_info=True)
            return False
        return True

    async def _load_geometry_data(self) -> Dict[str, Any]:
        """Load the active configured panel-group geometry."""
        if not self._db:
            return {} if True else None
        try:
            rows = await self._db.fetchall(
                "SELECT eg.display_name, eg.power_wp, eg.azimuth, eg.tilt,\n                          e.valid_from\n                   FROM panel_group_config_epochs e\n                   JOIN panel_group_config_epoch_groups eg\n                     ON eg.epoch_id = e.epoch_id\n                   WHERE e.valid_to IS NULL\n                     AND COALESCE(eg.is_active, 1) = 1\n                   ORDER BY eg.display_name"
            )
            if not rows:
                return (
                    {"groups": [], "metadata": {"system_capacity_kwp": 0.0}}
                    if True
                    else None
                )
            panel_groups = [
                {
                    "name": str(row[0]),
                    "power_kwp": float(row[1] or 0.0) / 1000.0,
                    "azimuth_deg": float(row[2]),
                    "tilt_deg": float(row[3]),
                }
                for row in rows
            ]
            return (
                {
                    "groups": panel_groups,
                    "metadata": {
                        "system_capacity_kwp": round(
                            sum(
                                (
                                    panel_group["power_kwp"]
                                    for panel_group in panel_groups
                                )
                            ),
                            3,
                        ),
                        "topology_valid_from": str(rows[0][4]) if rows[0][4] else None,
                    },
                }
                if True
                else None
            )
        except Exception as error:
            _LOGGER.error(f"Error loading geometry data: {error}")
            raise

    async def _load_statistics(self) -> Dict[str, Any]:
        """Load statistics from database. @zara"""
        if not self._db:
            return {}
        try:
            statistics = {}
            row = await self._db.fetchone(
                "SELECT peak_record_w, peak_record_date, peak_record_time\n                   FROM production_time_state\n                   WHERE id = 1 AND peak_record_w IS NOT NULL"
            )
            if row and row[0]:
                statistics["all_time_peak"] = {
                    "power_w": row[0],
                    "date": row[1],
                    "at": row[2],
                }
            summary_statistics = await self._db.get_daily_summary_statistics()
            statistics["total_production_kwh"] = (
                summary_statistics["total_yield_kwh"] or 0.0
            )
            statistics["avg_accuracy"] = summary_statistics["avg_accuracy"] or 0.0
            statistics["yield_days"] = summary_statistics["yield_days"]
            statistics["evaluation_days"] = summary_statistics["evaluation_days"]
            return statistics
        except Exception as error:
            _LOGGER.error(f"Error loading statistics: {error}")
            return {}

    async def _load_history(self) -> List[Dict[str, Any]]:
        """Load historical data from database. @zara"""
        if not self._db:
            return []
        try:
            rows = await self._db.fetchall(
                "SELECT date, actual_total_kwh, predicted_total_kwh,\n                          CASE WHEN evaluation_hours_count > 0\n                               THEN accuracy_percent END\n                   FROM daily_summaries\n                   WHERE actual_total_kwh IS NOT NULL\n                   ORDER BY date DESC\n                   LIMIT 365"
            )
            return [
                {
                    "date": row[0],
                    "actual_kwh": row[1],
                    "forecast_kwh": row[2],
                    "accuracy": row[3],
                }
                for row in rows
            ]
            pass
        except Exception as error:
            _LOGGER.error(f"Error loading history: {error}")
            return []

    async def _table_exists(self, table_name: str) -> bool:
        if not self._db:
            return False
        row = await self._db.fetchone(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
        return bool(row)

    async def _load_physics_telemetry(self) -> Dict[str, Any]:
        """Load diagnosis-only physics and GHI geometry evidence for the report."""
        empty_telemetry = {
            "contract_versions": [],
            "geometry": None,
            "sectors": [],
            "hourly_shape": [],
            "sample_kinds": [],
            "clamp_kinds": [],
        }
        if not self._db:
            return empty_telemetry
        try:
            telemetry = dict(empty_telemetry)
            if await self._table_exists("physics_calibration_groups"):
                rows = await self._db.fetchall(
                    "SELECT learning_contract_version, COUNT(*)\n                       FROM physics_calibration_groups\n                       GROUP BY learning_contract_version"
                )
                telemetry["contract_versions"] = [
                    {"version": row[0] or "unset", "groups": int(row[1] or 0)}
                    for row in rows
                ]
            if await self._table_exists("ghi_sensor_geometry_daily"):
                row = await self._db.fetchone(
                    "SELECT computed_on, lookback_days, sample_hours, bin_count,\n                              overlapping_elev_bins, max_east_west_k_p90_ratio,\n                              diagnosis\n                       FROM ghi_sensor_geometry_daily\n                       ORDER BY computed_on DESC\n                       LIMIT 1"
                )
                if row:
                    telemetry["geometry"] = {
                        "computed_on": row[0],
                        "lookback_days": row[1],
                        "sample_hours": row[2],
                        "bin_count": row[3],
                        "overlapping_elev_bins": row[4],
                        "max_east_west_k_p90_ratio": row[5],
                        "diagnosis": row[6],
                    }
            geometry = telemetry.get("geometry")
            if geometry:
                if await self._table_exists("ghi_sensor_geometry_sectors"):
                    rows = await self._db.fetchall(
                        "SELECT elev_bin, sector, sample_count, k_avg, k_p90, k_max\n                       FROM ghi_sensor_geometry_sectors\n                       WHERE computed_on = ?\n                       ORDER BY elev_bin, sector",
                        (geometry["computed_on"],),
                    )
                    telemetry["sectors"] = [
                        {
                            "elev_bin": int(row[0]),
                            "sector": row[1],
                            "sample_count": int(row[2] or 0),
                            "k_avg": row[3],
                            "k_p90": row[4],
                            "k_max": row[5],
                        }
                        for row in rows
                    ]
            if await self._table_exists("physics_calibration_hourly_shape"):
                rows = await self._db.fetchall(
                    "SELECT s.date, s.group_name, s.min_hour, s.max_hour,\n                              s.hour_count, s.min_factor, s.max_factor, s.spread\n                       FROM physics_calibration_hourly_shape s\n                       JOIN (\n                           SELECT group_name, MAX(date) AS date\n                           FROM physics_calibration_hourly_shape\n                           GROUP BY group_name\n                       ) latest\n                         ON latest.group_name = s.group_name\n                        AND latest.date = s.date\n                       ORDER BY s.group_name"
                )
                telemetry["hourly_shape"] = [
                    {
                        "date": row[0],
                        "group_name": row[1],
                        "min_hour": row[2],
                        "max_hour": row[3],
                        "hour_count": row[4],
                        "min_factor": row[5],
                        "max_factor": row[6],
                        "spread": row[7],
                    }
                    for row in rows
                ]
            if await self._table_exists("physics_calibration_samples"):
                rows = await self._db.fetchall(
                    "SELECT physics_ref_kind, COUNT(*), SUM(ratio_clamp_hit)\n                       FROM physics_calibration_samples\n                       GROUP BY physics_ref_kind\n                       ORDER BY physics_ref_kind"
                )
                telemetry["sample_kinds"] = [
                    {
                        "kind": row[0],
                        "samples": int(row[1] or 0),
                        "ratio_clamp_hits": int(row[2] or 0),
                    }
                    for row in rows
                ]
            if await self._table_exists("physics_calibration_clamp_hits"):
                rows = await self._db.fetchall(
                    "SELECT clamp_kind, COUNT(*)\n                       FROM physics_calibration_clamp_hits\n                       GROUP BY clamp_kind\n                       ORDER BY clamp_kind"
                )
                telemetry["clamp_kinds"] = [
                    {"kind": row[0], "count": int(row[1] or 0)} for row in rows
                ]
            return telemetry
        except Exception as error:
            _LOGGER.warning(
                "Physics telemetry unavailable for system report: %s", error
            )
            return empty_telemetry

    async def _write_report(self, content: str) -> None:
        """Write report to file asynchronously. @zara"""

        def _write_sync():
            self.docs_dir.mkdir(parents=True, exist_ok=True)
            with open(self.report_file, "w", encoding="utf-8") as report_file:
                report_file.write(content)
            return None if True else None

        event_loop = asyncio.get_running_loop()
        await event_loop.run_in_executor(None, _write_sync)

    def _build_report(
        self,
        geometry_data: Dict[str, Any],
        statistics: Dict[str, Any],
        history: List[Dict[str, Any]],
        physics_telemetry: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Build the Markdown report content. @zara"""
        now = datetime.now()
        panel_groups = geometry_data.get("groups", [])
        geometry_metadata = geometry_data.get("metadata", {})
        system_capacity_kwp = geometry_metadata.get("system_capacity_kwp", 0)
        record_peak = statistics.get("all_time_peak", {})
        record_peak_w = record_peak.get("power_w", 0)
        record_peak_date = record_peak.get("date", "N/A")
        total_production_kwh = statistics.get("total_production_kwh", 0)
        average_accuracy_percent = statistics.get("avg_accuracy", 0)
        seasonal_statistics = self._calculate_seasonal_stats(history)
        lines = [
            "# Solar Forecast ML - System Report",
            f"> Generated: {now.strftime('%Y-%m-%d %H:%M')} | System: {system_capacity_kwp} kWp",
            "",
            "---",
            "",
            "## Panel Geometry (configured)",
            "",
        ]
        if panel_groups:
            lines.extend(
                [
                    "| Group | Capacity | Tilt | Azimuth | Orientation |",
                    "|-------|----------|------|---------|-------------|",
                ]
            )
            for panel_group in panel_groups:
                lines.append(
                    f"| {panel_group['name']} | {panel_group['power_kwp']:.3f} kWp | {panel_group['tilt_deg']:.1f} deg | {panel_group['azimuth_deg']:.1f} deg | {self._azimuth_to_orientation(panel_group['azimuth_deg'])} |"
                )
            topology_valid_from = geometry_metadata.get("topology_valid_from")
            if topology_valid_from:
                lines.extend(["", f"**Topology valid from:** {topology_valid_from}"])
        else:
            lines.append("No active panel-group geometry is available.")
        lines.extend(
            [
                "",
                "---",
                "",
                "## Performance",
                "",
                f"**Record Peak:** {record_peak_w / 1000.0:.2f} kW ({record_peak_date})",
                "",
                f"**Total Production:** {total_production_kwh:.1f} kWh",
                "",
                f"**Average Accuracy:** {average_accuracy_percent:.1f}%",
                "",
                "### Seasonal Production",
                "",
                "| Season | Best Day | Avg Daily | Total |",
                "|--------|----------|-----------|-------|",
            ]
        )
        for season in ("Winter", "Spring", "Summer", "Autumn"):
            season_statistics = seasonal_statistics.get(season.lower(), {})
            best_day_kwh = season_statistics.get("best_day", 0)
            average_daily_kwh = season_statistics.get("avg_daily", 0)
            total_kwh = season_statistics.get("total", 0)
            days = season_statistics.get("days", 0)
            if days > 0:
                lines.append(
                    f"| {season} | {best_day_kwh:.2f} kWh | {average_daily_kwh:.2f} kWh | {total_kwh:.1f} kWh |"
                )
            else:
                lines.append(f"| {season} | - | - | - |")
        lines.extend(self._format_physics_telemetry_section(physics_telemetry or {}))
        quote = self._get_star_trek_quote()
        lines.extend(
            [
                "",
                "---",
                "",
                "## Message from the Captain's Log",
                "",
                f'> *"{quote}"*',
                "",
                "Live long and prosper!",
                "",
                "---",
                "",
                "*Report by Solar Forecast ML*",
                "",
                "*Created with solar power by [Zara-Toorox](https://github.com/Zara-Toorox)*",
            ]
        )
        return "\n".join(lines)

    def _format_physics_telemetry_section(self, telemetry: Dict[str, Any]) -> List[str]:
        """Append diagnosis-only physics/GHI evidence. This is the support channel."""
        from ..physics.physics_calibration_telemetry import (
            EAST_AZIMUTH_RANGE as east_azimuth_range,
            GHI_GEOMETRY_BEHAVIOR_MIN_BIN_SAMPLES as behavior_min_bin_samples,
            GHI_GEOMETRY_DIAGNOSIS_MIN_BIN_SAMPLES as diagnosis_min_bin_samples,
            GHI_GEOMETRY_HEADLINE_MIN_ELEV_BIN as headline_min_elevation_bin,
            GHI_GEOMETRY_LOOKBACK_DAYS as geometry_lookback_days,
            WEST_AZIMUTH_RANGE as west_azimuth_range,
            headline_eligible_elevations as headline_eligible_elevations,
            headline_exclusion_notes_from_sectors as headline_exclusion_notes_from_sectors,
        )

        east_min_azimuth, east_max_azimuth = east_azimuth_range
        west_min_azimuth, west_max_azimuth = west_azimuth_range
        lines = [
            "",
            "---",
            "",
            "## Physics calibration telemetry",
            "",
            "Diagnosis only. Living correction factors are unchanged.",
            "This section is the support evidence channel for fleet step-3 gates.",
            "",
        ]
        contract_versions = telemetry.get("contract_versions") or []
        if contract_versions:
            contract_summary = ", ".join(
                (
                    f"{contract_version['version']} ({contract_version['groups']} groups)"
                    for contract_version in contract_versions
                )
            )
            lines.append(f"**Learning contract:** {contract_summary}")
        else:
            lines.append("**Learning contract:** not yet written")
        lines.extend(["", "### GHI sensor geometry (diagnosis a)", ""])
        lines.append(
            f"Lookback window: {geometry_lookback_days} days (product constant, not a single-plant history length)."
        )
        lines.append(
            f"v1 contrast is east azimuth {east_min_azimuth:.0f}-{east_max_azimuth:.0f} vs west {west_min_azimuth:.0f}-{west_max_azimuth:.0f} at the same elevation bin. South-facing obstacles and southern-hemisphere morning/afternoon swap are not scored (those bins stay `other`)."
        )
        lines.append(
            f"Diagnosis bin floor: {diagnosis_min_bin_samples} samples. A later behavior/mask path (b) is not implemented and must use {behavior_min_bin_samples} samples (`CEILING_MIN_BIN_SAMPLES`), never the diagnosis floor."
        )
        lines.append(
            f"Headline east/west k_p90 pools raw samples per (elevation, sector); it uses elevation bins {headline_min_elevation_bin} and above. Lower overlapping pairs stay in the raw bins and the overlapping count. If those pairs are the only overlap, diagnosis is `contrast_below_headline_elevation` and the headline stays n/a."
        )
        lines.append("")
        geometry = telemetry.get("geometry")
        if geometry:
            east_west_p90_ratio = geometry.get("max_east_west_k_p90_ratio")
            ratio_text = (
                f"{float(east_west_p90_ratio):.2f}"
                if east_west_p90_ratio is not None
                else "n/a"
            )
            lines.extend(
                [
                    "| Computed on | Hours | Bins | Overlapping elev | Max east/west k_p90 | Diagnosis |",
                    "|-------------|-------|------|------------------|---------------------|-----------|",
                    f"| {geometry.get('computed_on')} | {geometry.get('sample_hours')} | {geometry.get('bin_count')} | {geometry.get('overlapping_elev_bins')} | {ratio_text} | {geometry.get('diagnosis')} |",
                    "",
                ]
            )
            headline_exclusions = headline_exclusion_notes_from_sectors(
                telemetry.get("sectors") or []
            )
            if headline_exclusions:
                lines.append("Headline exclusions:")
                for exclusion_note in headline_exclusions:
                    lines.append(f"- {exclusion_note}")
                lines.append("")
            sector_rows = telemetry.get("sectors") or []
            if sector_rows:
                headline_elevations = set(headline_eligible_elevations(sector_rows))
                lines.extend(
                    [
                        "| Elev | Sector | Hours | k_avg | k_p90 | k_max | Headline |",
                        "|------|--------|-------|-------|-------|-------|----------|",
                    ]
                )
                for row in sector_rows:
                    headline_eligible = int(row["elev_bin"]) in headline_elevations
                    k_avg = row.get("k_avg")
                    k_p90 = row.get("k_p90")
                    k_max = row.get("k_max")
                    lines.append(
                        f"| {row['elev_bin']} | {row['sector']} | {row['sample_count']} | {float(k_avg):.3f} | {float(k_p90):.3f} | {float(k_max):.3f} | {('yes' if headline_eligible else 'no')} |"
                    )
                lines.append("")
        else:
            lines.extend(
                [
                    "Not yet available. Needs local GHI actuals and one end-of-day run.",
                    "",
                ]
            )
        lines.extend(["### Hourly physics-factor shape", ""])
        hourly_shape_rows = telemetry.get("hourly_shape") or []
        if hourly_shape_rows:
            lines.extend(
                [
                    "| Group | Date | Hours | Min factor | Max factor | Spread |",
                    "|-------|------|-------|------------|------------|--------|",
                ]
            )
            for row in hourly_shape_rows:
                lines.append(
                    f"| {row['group_name']} | {row['date']} | {row['min_hour']}-{row['max_hour']} ({row['hour_count']}) | {float(row['min_factor']):.3f} | {float(row['max_factor']):.3f} | {float(row['spread']):.3f} |"
                )
            lines.append("")
        else:
            lines.extend(
                ["Not yet available. Needs one physics-calibration end-of-day run.", ""]
            )
        lines.extend(["### Sample ledger", ""])
        sample_kind_rows = telemetry.get("sample_kinds") or []
        if sample_kind_rows:
            lines.extend(
                [
                    "| Physics ref kind | Samples | Ratio clamp hits |",
                    "|------------------|---------|------------------|",
                ]
            )
            for row in sample_kind_rows:
                lines.append(
                    f"| {row['kind']} | {row['samples']} | {row['ratio_clamp_hits']} |"
                )
            lines.append("")
        else:
            lines.extend(["No sample-ledger rows yet.", ""])
        clamp_kind_rows = telemetry.get("clamp_kinds") or []
        if clamp_kind_rows:
            lines.extend(["| Clamp kind | Hits |", "|------------|------|"])
            for row in clamp_kind_rows:
                lines.append(f"| {row['kind']} | {row['count']} |")
            lines.append("")
        return lines

    def _get_star_trek_quote(self) -> str:
        """Return a random Star Trek quote related to energy/sun/exploration. @zara"""
        quotes = [
            "The sun is the source of all life. Even in the 24th century, we still look to the stars. - Captain Picard",
            "Infinite diversity in infinite combinations... including solar panel orientations. - Spock",
            "Make it so! And by 'it', I mean maximum solar efficiency. - Captain Picard",
            "Beam me up some photons, Scotty! - Captain Kirk (probably)",
            "Resistance to renewable energy is futile. - The Borg",
            "Logic dictates that harvesting solar energy is the most efficient course of action. - Spock",
            "I'm giving her all she's got, Captain! The panels are at maximum output! - Scotty",
            "Space: the final frontier. Solar panels: the home frontier. - Captain Kirk",
            "Today is a good day to generate clean energy! - Worf",
            "The needs of the many outweigh the needs of the few... use solar power. - Spock",
            "Engage... maximum solar absorption! - Captain Picard",
            "Fascinating. Your panel efficiency has improved by 2.6 degrees. - Spock",
        ]
        return random.choice(quotes)

    def _azimuth_to_orientation(self, azimuth: float) -> str:
        """Convert azimuth angle to cardinal direction. @zara"""
        azimuth = azimuth % 360
        orientations = [
            (0, "North"),
            (45, "North-East"),
            (90, "East"),
            (135, "South-East"),
            (180, "South"),
            (225, "South-West"),
            (270, "West"),
            (315, "North-West"),
            (360, "North"),
        ]
        for direction_azimuth, orientation in orientations:
            if not abs(azimuth - direction_azimuth) <= 22.5:
                continue
            return orientation
        return "South"

    def _calculate_seasonal_stats(
        self, history: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Calculate production statistics per season. @zara"""
        seasonal_statistics = {
            "winter": {"days": 0, "total": 0, "best_day": 0, "values": []},
            "spring": {"days": 0, "total": 0, "best_day": 0, "values": []},
            "summer": {"days": 0, "total": 0, "best_day": 0, "values": []},
            "autumn": {"days": 0, "total": 0, "best_day": 0, "values": []},
        }
        for history_row in history:
            date_text = history_row.get("date", "")
            actual_kwh = history_row.get("actual_kwh") or 0
            if not date_text or actual_kwh <= 0:
                continue
            try:
                history_date = datetime.strptime(date_text, "%Y-%m-%d")
                month = history_date.month
            except ValueError:
                continue
            season = None
            for season_name, season_months in SEASONS.items():
                if not month in season_months:
                    continue
                season = season_name
                break
            if not season:
                continue
            seasonal_statistics[season]["days"] += 1
            seasonal_statistics[season]["total"] += actual_kwh
            seasonal_statistics[season]["values"].append(actual_kwh)
            if not actual_kwh > seasonal_statistics[season]["best_day"]:
                continue
            seasonal_statistics[season]["best_day"] = actual_kwh
            continue
        for season in seasonal_statistics:
            if seasonal_statistics[season]["days"] > 0:
                seasonal_statistics[season]["avg_daily"] = (
                    seasonal_statistics[season]["total"]
                    / seasonal_statistics[season]["days"]
                )
            else:
                seasonal_statistics[season]["avg_daily"] = 0
        return seasonal_statistics

    async def get_quick_stats(self) -> Dict[str, Any]:
        """Get quick statistics without generating full report. @zara"""
        if not self._db:
            return {}
        try:
            quick_statistics = {}
            from ..core.core_helpers import SafeDateTimeUtil as date_time_util

            today_date_text = date_time_util.now().date().isoformat()
            row = await self._db.fetchone(
                "SELECT SUM(actual_kwh)\n                   FROM hourly_predictions\n                   WHERE target_date = ? AND actual_kwh IS NOT NULL",
                (today_date_text,),
            )
            quick_statistics["today_actual_kwh"] = row[0] if row and row[0] else 0
            today = date_time_util.now().date()
            tomorrow_date_text = (today + timedelta(days=1)).isoformat()
            week_start_date_text = (today - timedelta(days=today.weekday())).isoformat()
            week_statistics = await self._db.get_daily_summary_statistics(
                start_date=week_start_date_text, end_date=tomorrow_date_text
            )
            quick_statistics["week_actual_kwh"] = (
                week_statistics["total_yield_kwh"] or 0
            )
            month_start_date_text = today.replace(day=1).isoformat()
            month_statistics = await self._db.get_daily_summary_statistics(
                start_date=month_start_date_text, end_date=tomorrow_date_text
            )
            quick_statistics["month_actual_kwh"] = (
                month_statistics["total_yield_kwh"] or 0
            )
            quick_statistics["month_avg_daily_kwh"] = (
                month_statistics["avg_yield_kwh"] or 0
            )
            quick_statistics["month_days_with_data"] = month_statistics["yield_days"]
            return quick_statistics
        except Exception as error:
            _LOGGER.error(f"Error getting quick stats: {error}")
            return {}


async def create_system_report_generator(
    data_dir: Path, db_manager: Optional[DatabaseManager] = None
) -> SystemReportGenerator:
    """Factory function to create SystemReportGenerator. @zara"""
    return SystemReportGenerator(data_dir, db_manager)
