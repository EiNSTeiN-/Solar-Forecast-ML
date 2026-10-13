# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Feature engineering for Multi-Output LSTM solar forecast model.
Extracts 28 normalized features from weather/astronomy/production data.
(25 base features + 3 group-specific features per panel group)

V16.2.0: Erweitert um 8 neue Features basierend auf Jürgen Benchmark-Analyse.
"""
import logging
import math
from typing import Any, Dict, List, Optional

_LOGGER = logging.getLogger(__name__)


class FeatureEngineer:
    """Extract normalized features for Multi-Output AI @zara"""

    BASE_FEATURE_NAMES = [
        "hour_of_day",
        "day_of_year",
        "season_encoded",
        "weather_temp",
        "weather_ghi",
        "weather_wind",
        "weather_humidity",
        "weather_rain",
        "weather_clouds",
        "weather_pressure",
        "weather_dni",
        "weather_diffuse",
        "clear_sky_radiation",
        "dni_ratio",
        "sun_elevation_deg",
        "sun_azimuth_deg",
        "theoretical_max_kwh",
        "elevation_normalized",
        "daylight_hours",
        "day_progress_ratio",
        "hours_after_sunrise",
        "hours_before_sunset",
        "hours_since_solar_noon",
        "production_yesterday",
        "production_same_hour",
        "cloud_delta",
        "pressure_tendency_24h",
    ]
    GROUP_FEATURE_NAMES = [
        "group_azimuth_normalized",
        "group_tilt_normalized",
        "group_capacity_normalized",
    ]
    FEATURE_NAMES = BASE_FEATURE_NAMES + GROUP_FEATURE_NAMES
    FEATURE_RANGES = {
        "hour_of_day": (0, 23),
        "day_of_year": (1, 365),
        "season_encoded": (0, 3),
        "weather_temp": (-20, 45),
        "weather_ghi": (0, 1200),
        "weather_wind": (0, 30),
        "weather_humidity": (0, 100),
        "weather_rain": (0, 50),
        "weather_clouds": (0, 100),
        "weather_pressure": (950, 1050),
        "weather_dni": (0, 1000),
        "weather_diffuse": (0, 600),
        "clear_sky_radiation": (0, 1200),
        "dni_ratio": (0, 1.5),
        "sun_elevation_deg": (-10, 90),
        "sun_azimuth_deg": (0, 360),
        "theoretical_max_kwh": (0, 2),
        "elevation_normalized": (0, 1),
        "daylight_hours": (0, 18),
        "day_progress_ratio": (0, 1),
        "hours_after_sunrise": (0, 12),
        "hours_before_sunset": (0, 12),
        "hours_since_solar_noon": (-8, 8),
        "production_yesterday": (0, 20),
        "production_same_hour": (0, 3),
        "cloud_delta": (-1, 1),
        "group_azimuth_normalized": (0, 1),
        "group_tilt_normalized": (0, 1),
        "group_capacity_normalized": (0, 1),
    }

    def __init__(self):
        _LOGGER.debug(
            f"FeatureEngineer initialized: {len(self.FEATURE_NAMES)} features"
        )

    @property
    def feature_names(self) -> List[str]:
        """Feature names @zara"""
        return self.FEATURE_NAMES

    @property
    def feature_count(self) -> int:
        """Number of features @zara"""
        return len(self.FEATURE_NAMES)

    def extract_base_features(self, record: Dict[str, Any]) -> Optional[List[float]]:
        """Extract 25 base normalized features without group-specific @zara

        V16.0.0: Erweitert um 8 neue Features basierend auf Jürgen BM-Analyse:
        - weather_pressure, weather_diffuse (Wetter)
        - sun_azimuth_deg, daylight_hours, day_progress_ratio (Astronomie)
        - hours_after_sunrise, hours_before_sunset, hours_since_solar_noon (Zeit)
        """
        try:
            hour_of_day = float(record.get("target_hour", 12))
            day_of_year = float(record.get("target_day_of_year", 180))
            month = int(record.get("target_month", 6))
            season = record.get("target_season", "")
            if season:
                season_codes = {"winter": 0, "spring": 1, "summer": 2, "autumn": 3}
                season_encoded = float(season_codes.get(season, 2))
            elif month in (12, 1, 2):
                season_encoded = 0.0
            elif month in (3, 4, 5):
                season_encoded = 1.0
            elif month in (6, 7, 8):
                season_encoded = 2.0
            else:
                season_encoded = 3.0
            weather = record.get("weather_corrected", {})
            temperature_c = self._safe_float(weather.get("temperature"), 15.0)
            ghi_wm2 = self._safe_float(weather.get("solar_radiation_wm2"), 0.0)
            wind_ms = self._safe_float(weather.get("wind"), 3.0)
            humidity_percent = self._safe_float(weather.get("humidity"), 70.0)
            rain_mm = self._safe_float(weather.get("rain"), 0.0)
            clouds_percent = self._safe_float(weather.get("clouds"), 50.0)
            pressure_hpa = self._safe_float(weather.get("pressure"), 1013.0)
            dni_wm2 = self._safe_float(weather.get("dni"), 0.0)
            diffuse_wm2 = self._safe_float(weather.get("diffuse_radiation"), 0.0)
            astronomy = record.get("astronomy", {})
            sun_elevation_deg = self._safe_float(
                astronomy.get("sun_elevation_deg"), -30.0
            )
            sun_azimuth_deg = self._safe_float(astronomy.get("sun_azimuth_deg"), 180.0)
            theoretical_max_kwh = self._safe_float(
                astronomy.get("theoretical_max_kwh"), 0.0
            )
            clear_sky_wm2 = self._safe_float(
                astronomy.get("clear_sky_radiation_wm2"), 0.0
            )
            max_elevation_deg = self._safe_float(
                astronomy.get("max_elevation_today"), 60.0
            )
            daylight_hours = self._safe_float(astronomy.get("daylight_hours"), 12.0)
            day_progress_ratio = self._safe_float(
                astronomy.get("day_progress_ratio"), 0.5
            )
            hours_after_sunrise = self._safe_float(
                astronomy.get("hours_after_sunrise"), 6.0
            )
            hours_before_sunset = self._safe_float(
                astronomy.get("hours_before_sunset"), 6.0
            )
            hours_since_solar_noon = self._safe_float(
                astronomy.get("hours_since_solar_noon"), 0.0
            )
            production_yesterday_kwh = self._safe_float(
                record.get("production_yesterday"), 0.0
            )
            production_same_hour_kwh = self._safe_float(
                record.get("production_same_hour_yesterday"), 0.0
            )
            cloud_delta = self._safe_float(record.get("cloud_delta"), 0.0)
            pressure_tendency_24h = self._safe_float(
                record.get("pressure_tendency_24h"), 0.0
            )
            dni_ratio = self._calculate_dni_ratio(dni_wm2, clear_sky_wm2)
            elevation_normalized = (
                sun_elevation_deg / max_elevation_deg if max_elevation_deg > 0 else 0.0
            )
            elevation_normalized = max(0.0, min(1.0, elevation_normalized))
            raw_features = [
                hour_of_day,
                day_of_year,
                season_encoded,
                temperature_c,
                ghi_wm2,
                wind_ms,
                humidity_percent,
                rain_mm,
                clouds_percent,
                pressure_hpa,
                dni_wm2,
                diffuse_wm2,
                clear_sky_wm2,
                dni_ratio,
                sun_elevation_deg,
                sun_azimuth_deg,
                theoretical_max_kwh,
                elevation_normalized,
                daylight_hours,
                day_progress_ratio,
                hours_after_sunrise,
                hours_before_sunset,
                hours_since_solar_noon,
                production_yesterday_kwh,
                production_same_hour_kwh,
                cloud_delta,
                pressure_tendency_24h,
            ]
            if len(raw_features) != len(self.BASE_FEATURE_NAMES):
                _LOGGER.error(
                    f"Base feature count mismatch: {len(raw_features)} vs {len(self.BASE_FEATURE_NAMES)}"
                )
                return
            normalized_features = []
            for raw_value, feature_name in zip(raw_features, self.BASE_FEATURE_NAMES):
                if (
                    not isinstance(raw_value, (int, float))
                    or math.isnan(raw_value)
                    or math.isinf(raw_value)
                ):
                    _LOGGER.warning(f"Invalid {feature_name}: {raw_value}, using 0.5")
                    normalized_features.append(0.5)
                else:
                    range_min, range_max = self.FEATURE_RANGES.get(feature_name, (0, 1))
                    normalized_value = (
                        (raw_value - range_min) / (range_max - range_min)
                        if range_max > range_min
                        else 0.5
                    )
                    normalized_value = max(0.0, min(1.0, normalized_value))
                    normalized_features.append(normalized_value)
            return normalized_features
        except Exception as error:
            _LOGGER.error(f"Base feature extraction failed: {error}")
            return

    def extract_group_features(
        self,
        group_azimuth: float,
        group_tilt: float,
        group_capacity_kwp: float,
        total_capacity_kwp: float,
    ) -> List[float]:
        """Extract 3 normalized group-specific features @zara"""
        azimuth_normalized = max(0.0, min(1.0, group_azimuth / 360.0))
        tilt_normalized = max(0.0, min(1.0, group_tilt / 90.0))
        if total_capacity_kwp > 0:
            capacity_normalized = max(
                0.0, min(1.0, group_capacity_kwp / total_capacity_kwp)
            )
        else:
            capacity_normalized = 0.5
        return [azimuth_normalized, tilt_normalized, capacity_normalized]

    def extract(
        self,
        record: Dict[str, Any],
        group_azimuth: float = 180.0,
        group_tilt: float = 30.0,
        group_capacity_kwp: float = 1.0,
        total_capacity_kwp: float = 1.0,
    ) -> Optional[List[float]]:
        """Extract full 28-feature vector for a specific panel group (25 base + 3 group) @zara"""
        base_features = self.extract_base_features(record)
        if base_features is None:
            return
        group_features = self.extract_group_features(
            group_azimuth, group_tilt, group_capacity_kwp, total_capacity_kwp
        )
        return base_features + group_features

    def extract_for_all_groups(
        self, record: Dict[str, Any], panel_groups: List[Dict[str, Any]]
    ) -> Optional[List[List[float]]]:
        """Extract 28-feature vectors for all panel groups (25 base + 3 group) @zara"""
        base_features = self.extract_base_features(record)
        if base_features is None:
            return
        total_capacity_kwp = sum(
            (
                self._get_group_capacity_kwp(capacity_group)
                for capacity_group in panel_groups
            )
        )
        group_vectors = []
        for panel_group in panel_groups:
            sun_azimuth_deg = self._safe_float(panel_group.get("azimuth", 180.0), 180.0)
            group_tilt_deg = self._safe_float(panel_group.get("tilt", 30.0), 30.0)
            group_capacity_kwp = self._get_group_capacity_kwp(panel_group)
            group_features = self.extract_group_features(
                sun_azimuth_deg, group_tilt_deg, group_capacity_kwp, total_capacity_kwp
            )
            group_vectors.append(base_features + group_features)
        return group_vectors

    def extract_combined_for_multi_output(
        self, record: Dict[str, Any], panel_groups: List[Dict[str, Any]]
    ) -> Optional[List[float]]:
        """Extract combined feature vector for Multi-Output LSTM @zara"""
        base_features = self.extract_base_features(record)
        if base_features is None:
            return
        if not panel_groups:
            return base_features
        total_capacity_kwp = sum(
            (
                self._get_group_capacity_kwp(capacity_group)
                for capacity_group in panel_groups
            )
        )
        combined_features = list(base_features)
        for panel_group in panel_groups:
            sun_azimuth_deg = self._safe_float(panel_group.get("azimuth", 180.0), 180.0)
            group_tilt_deg = self._safe_float(panel_group.get("tilt", 30.0), 30.0)
            group_capacity_kwp = self._get_group_capacity_kwp(panel_group)
            group_features = self.extract_group_features(
                sun_azimuth_deg, group_tilt_deg, group_capacity_kwp, total_capacity_kwp
            )
            combined_features.extend(group_features)
        return combined_features

    def get_combined_feature_count(self, num_groups: int) -> int:
        """Calculate total feature count for combined multi-output @zara"""
        if num_groups <= 0:
            return len(self.BASE_FEATURE_NAMES)
        return len(self.BASE_FEATURE_NAMES) + len(self.GROUP_FEATURE_NAMES) * num_groups

    def _get_group_capacity_kwp(self, group: Dict[str, Any]) -> float:
        """Get group capacity in kWp from various config formats @zara"""
        if "capacity_kwp" in group:
            return self._safe_float(group["capacity_kwp"], 1.0)
        if "kwp" in group:
            return self._safe_float(group["kwp"], 1.0)
        if "power_wp" in group:
            return self._safe_float(group["power_wp"], 1000.0) / 1000.0
        return 1.0

    def _safe_float(self, value: Any, default: float) -> float:
        """Convert value to float with fallback @zara"""
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def _calculate_dni_ratio(self, dni: float, clear_sky: float) -> float:
        """Calculate DNI ratio against clear-sky radiation."""
        if clear_sky > 1.0:
            return min(1.5, max(0.0, dni / clear_sky))
        return 0.5

    def get_defaults(self) -> List[float]:
        """Return default normalized feature vector @zara"""
        return [0.5] * len(self.FEATURE_NAMES)

    def validate(self, record: Dict[str, Any]) -> bool:
        """Check if record has required fields @zara"""
        required_fields = [
            "target_hour",
            "target_day_of_year",
            "weather_corrected",
            "astronomy",
        ]
        for field_name in required_fields:
            if not field_name not in record:
                continue
            _LOGGER.warning(f"Missing field: {field_name}")
            return False
        weather = record.get("weather_corrected", {})
        required_weather_fields = [
            "temperature",
            "solar_radiation_wm2",
            "wind",
            "humidity",
            "clouds",
        ]
        for field_name in required_weather_fields:
            if not field_name not in weather:
                continue
            _LOGGER.warning(f"Missing weather field: {field_name}")
            return False
        astronomy = record.get("astronomy", {})
        required_astronomy_fields = [
            "sun_elevation_deg",
            "theoretical_max_kwh",
            "clear_sky_radiation_wm2",
        ]
        for field_name in required_astronomy_fields:
            if not field_name not in astronomy:
                continue
            _LOGGER.warning(f"Missing astronomy field: {field_name}")
            return False
        return True
