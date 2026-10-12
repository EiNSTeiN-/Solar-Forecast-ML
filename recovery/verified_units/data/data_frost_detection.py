# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
Frost Detection for Solar Forecast ML V16.2.0.
Detects frost/ice on solar panels based on meteorological conditions.
Uses database operations for persistence.

@zara
"""
import logging
import math
from typing import Any, Dict, Optional
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from .data_io import DataManagerIO

_LOGGER = logging.getLogger(__name__)


def estimate_frost_from_forecast(
    temperature_c: Optional[float],
    humidity_percent: Optional[float],
    wind_speed_ms: Optional[float] = None,
    hour: Optional[int] = None,
) -> tuple[Optional[str], Optional[int]]:
    """Estimate frost conditions from weather forecast data. @zara

    Lightweight frost estimation for forecast-time bucket classification.
    Uses the same scoring components as FrostDetector but without radiation
    data (which is only available from actual sensor readings).

    Scoring (max 9, mirroring FrostDetector thresholds):
    - Temperature score (0-3): Same as FrostDetector
    - Humidity/dewpoint score (0-2): Same as FrostDetector
    - Wind score (0-2): Same as FrostDetector
    - Frost probability bonus (0-2): From _calculate_frost_probability logic

    Args:
        temperature_c: Forecast temperature in Celsius
        humidity_percent: Forecast relative humidity (0-100)
        wind_speed_ms: Optional wind speed in m/s
        hour: Optional hour (0-23)

    Returns:
        Tuple of (frost_detected, frost_score):
        - frost_detected: "heavy_frost", "light_frost", or None
        - frost_score: 0-10 integer, or None if insufficient data
    """
    if temperature_c is None or humidity_percent is None:
        return (None, None)
    if temperature_c > 3.0:
        return (None, 0)
    clamped_humidity = max(1.0, min(100.0, humidity_percent))
    try:
        magnus_a = 17.27
        magnus_b = 237.7
        magnus_gamma = magnus_a * temperature_c / (magnus_b + temperature_c) + math.log(
            clamped_humidity / 100.0
        )
        dewpoint_c = magnus_b * magnus_gamma / (magnus_a - magnus_gamma)
    except (ValueError, ZeroDivisionError):
        dewpoint_c = temperature_c - (100 - clamped_humidity) / 5.0
    if dewpoint_c > 0:
        return (None, 0)
    frost_margin_c = temperature_c - dewpoint_c
    temperature_score = 0
    if temperature_c < -5:
        temperature_score = 3
    elif temperature_c < -2:
        temperature_score = 2
    elif temperature_c < 0:
        temperature_score = 1
    humidity_score = 0
    if frost_margin_c < 1.0 and dewpoint_c < 0:
        humidity_score = 2
    elif frost_margin_c < 2.0 and dewpoint_c < 0:
        humidity_score = 2
    elif humidity_percent > 85:
        humidity_score = 2
    elif frost_margin_c < 3.0 and dewpoint_c < 0:
        humidity_score = 1
    elif humidity_percent > 75:
        humidity_score = 1
    wind_score = 0
    if wind_speed_ms is not None:
        if wind_speed_ms < 1:
            wind_score = 2
        elif wind_speed_ms < 3:
            wind_score = 1
    frost_probability = 0.0
    if frost_margin_c < 0:
        frost_probability = 0.95
    elif frost_margin_c < 1.0:
        frost_probability = 0.85
    elif frost_margin_c < 2.0:
        frost_probability = 0.65
    elif frost_margin_c < 3.0:
        frost_probability = 0.4
    elif frost_margin_c < 5.0:
        frost_probability = 0.15
    if wind_speed_ms is not None and wind_speed_ms > 4.0:
        frost_probability *= 0.3
    elif wind_speed_ms is not None and wind_speed_ms > 2.0:
        frost_probability *= 0.6
    probability_bonus = 0
    if frost_probability > 0.8:
        probability_bonus = 2
    elif frost_probability > 0.5:
        probability_bonus = 1
    frost_score = min(
        10, temperature_score + humidity_score + wind_score + probability_bonus
    )
    if frost_score >= FrostDetector.FROST_SCORE_THRESHOLD_HEAVY:
        return ("heavy_frost", frost_score)
    if frost_score >= FrostDetector.FROST_SCORE_THRESHOLD_LIGHT:
        return ("light_frost", frost_score)
    return (None, frost_score)


class FrostDetector(DataManagerIO):
    """Detects frost/ice on solar panels using meteorological conditions. @zara

    V16.0.0 Enhanced Algorithm:
    1. Dewpoint calculation (Magnus formula) for physical frost probability
    2. Frost margin analysis (temperature - dewpoint)
    3. Correlation-based detection: Compare actual radiation drop vs expected cloud drop
    4. Dynamic thresholds based on frost conditions and wind
    5. Combined scoring (0-10) with physics-based validation
    6. Database persistence for frost events

    Detection Logic:
    - Frost forms when surface temperature < dewpoint AND dewpoint < 0C
    - Correlation detection: If radiation drops MORE than clouds explain -> FROST
    - Wind factor: Low wind enables frost formation

    Score >= 8: Heavy frost (exclude from ML training)
    Score 5-7: Light frost (monitor)
    Score < 5: No frost
    """

    FROST_SCORE_THRESHOLD_HEAVY = 8
    FROST_SCORE_THRESHOLD_LIGHT = 5
    CORRELATION_THRESHOLD_BASE = 0.2

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        """Initialize frost detector. @zara

        Args:
            hass: Home Assistant instance
            db_manager: DatabaseManager instance for DB operations
        """
        super().__init__(hass, db_manager)
        _LOGGER.debug("FrostDetector initialized with DatabaseManager")

    async def detect_frost(
        self,
        temperature_c: Optional[float],
        humidity_percent: Optional[float],
        wind_speed_ms: Optional[float],
        solar_radiation_wm2: Optional[float],
        theoretical_max_wm2: Optional[float],
        cloud_cover_percent: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Detect frost on solar panels using enhanced correlation-based algorithm. @zara

        Args:
            temperature_c: Air temperature in Celsius
            humidity_percent: Relative humidity (0-100)
            wind_speed_ms: Wind speed in m/s
            solar_radiation_wm2: Measured solar radiation (W/m2)
            theoretical_max_wm2: Theoretical max radiation from astronomy (W/m2)
            cloud_cover_percent: Cloud coverage 0-100% (optional, from weather)

        Returns:
            Dictionary with:
                - frost_detected: None | "light_frost" | "heavy_frost"
                - frost_score: Score 0-10
                - confidence: 0.0-1.0
                - actual_vs_expected_pct: Radiation comparison
                - indicators: Dict with individual metrics
                - reason: Human-readable explanation
                - frost_analysis: Dict with correlation analysis
        """
        result = {
            "frost_detected": None,
            "frost_score": 0,
            "confidence": 0.0,
            "actual_vs_expected_pct": None,
            "indicators": {},
            "reason": None,
            "frost_analysis": {
                "dewpoint_c": None,
                "frost_margin_c": None,
                "frost_probability": 0.0,
                "correlation_diff_percent": None,
                "threshold_used_percent": None,
                "detection_method": "correlation_enhanced",
                "wind_frost_factor": None,
                "physical_frost_possible": False,
            },
        }
        if any(
            (
                value is None
                for value in (
                    temperature_c,
                    humidity_percent,
                    wind_speed_ms,
                    solar_radiation_wm2,
                    theoretical_max_wm2,
                )
            )
        ):
            result["reason"] = "Missing sensor data"
            result["frost_analysis"]["detection_method"] = "skipped_missing_data"
            return result
        if theoretical_max_wm2 < 5:
            result["reason"] = "Nighttime (no solar radiation expected)"
            result["frost_analysis"]["detection_method"] = "skipped_nighttime"
            return result
        dewpoint_c = self._calculate_dewpoint(temperature_c, humidity_percent)
        result["frost_analysis"]["dewpoint_c"] = round(dewpoint_c, 2)
        frost_margin_c = temperature_c - dewpoint_c
        result["frost_analysis"]["frost_margin_c"] = round(frost_margin_c, 2)
        physical_frost_possible = dewpoint_c < 0 and frost_margin_c < 3.0
        result["frost_analysis"]["physical_frost_possible"] = physical_frost_possible
        wind_frost_factor = self._calculate_wind_frost_factor(wind_speed_ms)
        result["frost_analysis"]["wind_frost_factor"] = round(wind_frost_factor, 2)
        frost_probability = self._calculate_frost_probability(
            temperature_c, dewpoint_c, frost_margin_c, wind_frost_factor
        )
        result["frost_analysis"]["frost_probability"] = round(frost_probability, 3)
        if temperature_c > 3 or (temperature_c > 1 and dewpoint_c > 0):
            result["reason"] = (
                f"No frost conditions (temp={temperature_c:.1f}C, dewpoint={dewpoint_c:.1f}C)"
            )
            result["frost_analysis"]["detection_method"] = "ruled_out_physics"
            return result
        expected_radiation = self._calculate_expected_radiation(
            theoretical_max_wm2, cloud_cover_percent
        )
        if expected_radiation < 3:
            result["reason"] = (
                f"Expected radiation too low ({expected_radiation:.1f} W/m2)"
            )
            result["frost_analysis"]["detection_method"] = "skipped_low_radiation"
            return result
        expected_drop = (
            1.0 - expected_radiation / theoretical_max_wm2
            if theoretical_max_wm2 > 0
            else 0
        )
        actual_drop = (
            1.0 - solar_radiation_wm2 / theoretical_max_wm2
            if theoretical_max_wm2 > 0
            else 0
        )
        expected_drop = max(0.0, min(1.0, expected_drop))
        actual_drop = max(0.0, min(1.0, actual_drop))
        correlation_difference = actual_drop - expected_drop
        result["frost_analysis"]["correlation_diff_percent"] = round(
            correlation_difference * 100, 1
        )
        actual_vs_expected = (
            solar_radiation_wm2 / expected_radiation if expected_radiation > 0 else 1.0
        )
        result["actual_vs_expected_pct"] = round(actual_vs_expected * 100, 1)
        threshold = self._calculate_dynamic_threshold(
            frost_probability, wind_frost_factor, cloud_cover_percent
        )
        result["frost_analysis"]["threshold_used_percent"] = round(threshold * 100, 1)
        frost_score = 0
        temperature_score = 0
        if temperature_c < -5:
            temperature_score = 3
        elif temperature_c < -2:
            temperature_score = 2
        elif temperature_c < 0:
            temperature_score = 1
        frost_score += temperature_score
        humidity_score = 0
        if frost_margin_c < 1.0 and dewpoint_c < 0:
            humidity_score = 2
        elif frost_margin_c < 2.0 and dewpoint_c < 0:
            humidity_score = 2
        elif humidity_percent > 85:
            humidity_score = 2
        elif frost_margin_c < 3.0 and dewpoint_c < 0:
            humidity_score = 1
        elif humidity_percent > 75:
            humidity_score = 1
        frost_score += humidity_score
        wind_score = 0
        if wind_speed_ms < 1:
            wind_score = 2
        elif wind_speed_ms < 3:
            wind_score = 1
        frost_score += wind_score
        radiation_score = 0
        if correlation_difference > threshold and physical_frost_possible:
            if correlation_difference > 0.4:
                radiation_score = 3
            elif correlation_difference > 0.25:
                radiation_score = 2
            elif correlation_difference > threshold:
                radiation_score = 1
        elif actual_vs_expected < 0.4:
            radiation_score = 3
        elif actual_vs_expected < 0.6:
            radiation_score = 2
        elif actual_vs_expected < 0.8:
            radiation_score = 1
        frost_score += radiation_score
        probability_bonus = 0
        if frost_probability > 0.8:
            probability_bonus = 2
        elif frost_probability > 0.5:
            probability_bonus = 1
        frost_score += probability_bonus
        frost_score = min(10, frost_score)
        result["frost_score"] = frost_score
        result["indicators"] = {
            "temperature_c": temperature_c,
            "humidity_percent": humidity_percent,
            "wind_speed_ms": wind_speed_ms,
            "radiation_deficit_pct": round((1 - actual_vs_expected) * 100, 1),
            "temp_score": temperature_score,
            "humidity_score": humidity_score,
            "wind_score": wind_score,
            "radiation_score": radiation_score,
            "probability_bonus": probability_bonus,
            "dewpoint_c": round(dewpoint_c, 1),
            "frost_margin_c": round(frost_margin_c, 1),
        }
        if frost_score >= self.FROST_SCORE_THRESHOLD_HEAVY:
            result["frost_detected"] = "heavy_frost"
            result["confidence"] = min(1.0, frost_score / 10)
            if correlation_difference > threshold and physical_frost_possible:
                detection_basis = f"correlation-based (unexplained drop {correlation_difference * 100:.0f}%)"
            else:
                detection_basis = (
                    f"radiation deficit {(1 - actual_vs_expected) * 100:.0f}%"
                )
            result["reason"] = (
                f"Heavy frost detected (score {frost_score}/10, {detection_basis}): temp={temperature_c:.1f}C, dewpoint={dewpoint_c:.1f}C, frost_margin={frost_margin_c:.1f}C, wind={wind_speed_ms:.1f}m/s"
            )
        elif frost_score >= self.FROST_SCORE_THRESHOLD_LIGHT:
            result["frost_detected"] = "light_frost"
            result["confidence"] = frost_score / 10
            result["reason"] = (
                f"Light frost detected (score {frost_score}/10): temp={temperature_c:.1f}C, dewpoint={dewpoint_c:.1f}C, frost_margin={frost_margin_c:.1f}C"
            )
        elif physical_frost_possible:
            result["reason"] = (
                f"Frost conditions present but no significant impact (score {frost_score}/10): dewpoint={dewpoint_c:.1f}C, margin={frost_margin_c:.1f}C"
            )
        else:
            result["reason"] = f"No frost detected (score {frost_score}/10)"
        return result if True else None

    def _calculate_dewpoint(
        self, temperature_c: float, humidity_percent: float
    ) -> float:
        """Calculate dewpoint temperature using Magnus formula. @zara

        Args:
            temperature_c: Air temperature in Celsius
            humidity_percent: Relative humidity (0-100)

        Returns:
            Dewpoint temperature in Celsius
        """
        magnus_a = 17.27
        magnus_b = 237.7
        humidity_percent = max(1.0, min(100.0, humidity_percent))
        try:
            magnus_gamma = magnus_a * temperature_c / (
                magnus_b + temperature_c
            ) + math.log(humidity_percent / 100.0)
            calculated_dewpoint = magnus_b * magnus_gamma / (magnus_a - magnus_gamma)
            return calculated_dewpoint
        except (ValueError, ZeroDivisionError):
            return temperature_c - (100 - humidity_percent) / 5.0

    def _calculate_wind_frost_factor(self, wind_speed_ms: float) -> float:
        """Calculate wind influence on frost formation. @zara

        Low wind allows radiative cooling which enables frost formation.

        Args:
            wind_speed_ms: Wind speed in m/s

        Returns:
            Wind frost factor (0-1, higher = more favorable for frost)
        """
        if wind_speed_ms < 0.5:
            return 1.0
        if wind_speed_ms < 1.5:
            return 0.85
        if wind_speed_ms < 2.5:
            return 0.65
        if wind_speed_ms < 4.0:
            return 0.4
        if wind_speed_ms < 6.0:
            return 0.2
        return 0.05

    def _calculate_frost_probability(
        self,
        temperature_c: float,
        dewpoint_c: float,
        frost_margin: float,
        wind_factor: float,
    ) -> float:
        """Calculate physical frost probability based on conditions. @zara

        Frost formation requires:
        1. Dewpoint below 0C (sublimation instead of condensation)
        2. Surface temperature approaching dewpoint
        3. Low wind (allows radiative cooling)

        Args:
            temperature_c: Air temperature in Celsius
            dewpoint_c: Calculated dewpoint in Celsius
            frost_margin: Temperature - Dewpoint difference
            wind_factor: Wind influence factor (0-1)

        Returns:
            Probability 0.0-1.0
        """
        if dewpoint_c > 0:
            return 0.0
        if temperature_c > 3:
            return 0.0
        if frost_margin < 0:
            margin_probability = 0.95
        elif frost_margin < 1.0:
            margin_probability = 0.9
        elif frost_margin < 2.0:
            margin_probability = 0.75
        elif frost_margin < 3.0:
            margin_probability = 0.5
        elif frost_margin < 5.0:
            margin_probability = 0.25
        else:
            margin_probability = 0.05
        if temperature_c < -5:
            temperature_factor = 1.0
        elif temperature_c < -2:
            temperature_factor = 0.9
        elif temperature_c < 0:
            temperature_factor = 0.8
        elif temperature_c < 2:
            temperature_factor = 0.5
        else:
            temperature_factor = 0.2
        probability = margin_probability * temperature_factor * wind_factor
        return min(1.0, max(0.0, probability))

    def _calculate_dynamic_threshold(
        self,
        frost_probability: float,
        wind_factor: float,
        cloud_cover_percent: Optional[float],
    ) -> float:
        """Calculate dynamic correlation threshold based on conditions. @zara

        Lower threshold = more sensitive to frost detection
        Higher threshold = requires more evidence

        Args:
            frost_probability: Physical frost probability (0-1)
            wind_factor: Wind influence factor (0-1)
            cloud_cover_percent: Cloud coverage (0-100) or None

        Returns:
            Threshold for correlation difference (0.05-0.35)
        """
        threshold = self.CORRELATION_THRESHOLD_BASE
        if frost_probability > 0.7:
            threshold -= 0.08
        elif frost_probability > 0.4:
            threshold -= 0.04
        if wind_factor > 0.8:
            threshold -= 0.04
        if cloud_cover_percent is not None:
            if cloud_cover_percent > 85:
                threshold += 0.08
            elif cloud_cover_percent > 70:
                threshold += 0.04
        return max(0.05, min(0.35, threshold))

    def _calculate_expected_radiation(
        self, theoretical_max_wm2: float, cloud_cover_percent: Optional[float]
    ) -> float:
        """Calculate expected radiation based on cloud cover. @zara

        Uses exponential cloud transmission model.

        Args:
            theoretical_max_wm2: Clear-sky maximum radiation
            cloud_cover_percent: Cloud coverage (0-100) or None

        Returns:
            Expected radiation in W/m2
        """
        if cloud_cover_percent is None:
            cloud_cover_percent = 50.0
        cloud_factor = math.exp(-0.008 * cloud_cover_percent)
        expected_radiation = theoretical_max_wm2 * cloud_factor
        return expected_radiation

    def should_exclude_from_training(self, frost_result: Dict[str, Any]) -> bool:
        """Determine if hour should be excluded from AI training. @zara

        Args:
            frost_result: Result from detect_frost()

        Returns:
            True if heavy frost detected (exclude), False otherwise
        """
        return frost_result.get("frost_detected") == "heavy_frost"

    async def save_frost_event(
        self, date_str: str, hour: int, frost_result: Dict[str, Any]
    ) -> bool:
        """Save frost event to daily summary analysis table. @zara

        Args:
            date_str: Date string (YYYY-MM-DD)
            hour: Hour (0-23)
            frost_result: Result from detect_frost()

        Returns:
            True if saved successfully
        """
        try:
            frost_level = frost_result.get("frost_detected")
            if not frost_level or frost_level in ("none", False):
                return True
            existing_summary = await self.fetch_one(
                "SELECT total_affected_hours, heavy_frost_hours, light_frost_hours\n                   FROM daily_summary_frost_analysis WHERE date = ?",
                (date_str,),
            )
            detected_level = frost_result.get("frost_detected")
            if existing_summary:
                total_affected_hours = existing_summary[0] + 1
                heavy_frost_hours = (
                    existing_summary[1] + 1
                    if detected_level == "heavy_frost"
                    else existing_summary[1]
                )
                light_frost_hours = (
                    existing_summary[2] + 1
                    if detected_level == "light_frost"
                    else existing_summary[2]
                )
                await self.execute_query(
                    "UPDATE daily_summary_frost_analysis\n                       SET frost_detected = TRUE,\n                           total_affected_hours = ?,\n                           heavy_frost_hours = ?,\n                           light_frost_hours = ?\n                       WHERE date = ?",
                    (
                        total_affected_hours,
                        heavy_frost_hours,
                        light_frost_hours,
                        date_str,
                    ),
                )
            else:
                heavy_frost_hours = 1 if detected_level == "heavy_frost" else 0
                light_frost_hours = 1 if detected_level == "light_frost" else 0
                await self.execute_query(
                    "INSERT INTO daily_summary_frost_analysis\n                       (date, hours_analyzed, frost_detected, total_affected_hours,\n                        heavy_frost_hours, light_frost_hours)\n                       VALUES (?, 1, TRUE, 1, ?, ?)",
                    (date_str, heavy_frost_hours, light_frost_hours),
                )
            _LOGGER.debug(
                "Saved %s event for %s hour %d", detected_level, date_str, hour
            )
        except Exception as error:
            _LOGGER.error("Failed to save frost event: %s", error)
            return False
        return True

    async def get_frost_analysis_for_date(
        self, date_str: str
    ) -> Optional[Dict[str, Any]]:
        """Get frost analysis for a specific date from database. @zara

        Args:
            date_str: Date string (YYYY-MM-DD)

        Returns:
            Dictionary with frost analysis or None
        """
        try:
            analysis_row = await self.fetch_one(
                "SELECT hours_analyzed, frost_detected, total_affected_hours,\n                          heavy_frost_hours, light_frost_hours\n                   FROM daily_summary_frost_analysis WHERE date = ?",
                (date_str,),
            )
            if not analysis_row:
                return
            return {
                "date": date_str,
                "hours_analyzed": analysis_row[0],
                "frost_detected": bool(analysis_row[1]),
                "total_affected_hours": analysis_row[2],
                "heavy_frost_hours": analysis_row[3],
                "light_frost_hours": analysis_row[4],
            }
        except Exception as error:
            _LOGGER.error("Failed to get frost analysis: %s", error)
            return

    async def get_frost_stats(self, days: int = 30) -> Dict[str, Any]:
        """Get frost statistics for recent days. @zara

        Args:
            days: Number of days to analyze

        Returns:
            Dictionary with frost statistics
        """
        try:
            history_rows = await self.fetch_all(
                "SELECT date, frost_detected, total_affected_hours,\n                          heavy_frost_hours, light_frost_hours\n                   FROM daily_summary_frost_analysis\n                   WHERE date >= date('now', ?)\n                   ORDER BY date DESC",
                (f"-{days} days",),
            )
            frost_days = sum((1 for history_row in history_rows if history_row[1]))
            total_heavy_hours = sum(
                (history_row[3] or 0 for history_row in history_rows)
            )
            total_light_hours = sum(
                (history_row[4] or 0 for history_row in history_rows)
            )
            return (
                (
                    {
                        "days_analyzed": len(history_rows),
                        "frost_days": frost_days,
                        "total_heavy_frost_hours": total_heavy_hours,
                        "total_light_frost_hours": total_light_hours,
                        "frost_day_percentage": (
                            round(frost_days / len(history_rows) * 100, 1)
                            if history_rows
                            else 0
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
        except Exception as error:
            _LOGGER.error("Failed to get frost stats: %s", error)
            return {
                "days_analyzed": 0,
                "frost_days": 0,
                "total_heavy_frost_hours": 0,
                "total_light_frost_hours": 0,
                "error": str(error),
            }
