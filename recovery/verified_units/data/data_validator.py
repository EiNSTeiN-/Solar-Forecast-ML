# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
Data Validator for Solar Forecast ML V16.2.0.
Validates data integrity for ML and forecast data using database operations.
All validation now DB-based, no JSON file operations.

@zara
"""
import logging
from datetime import datetime
from typing import Any, Dict
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from .data_io import DataManagerIO
from ..const import DATA_VERSION, MIN_TRAINING_DATA_POINTS
from ..core.core_helpers import SafeDateTimeUtil

_LOGGER = logging.getLogger(__name__)


class DataValidator(DataManagerIO):
    """Validates data integrity for ML and forecast data. @zara

    All validation operations use database queries instead of JSON files.
    """

    def __init__(self, hass: HomeAssistant, db_manager: DatabaseManager):
        """Initialize the data validator. @zara

        Args:
            hass: Home Assistant instance
            db_manager: DatabaseManager instance for DB operations
        """
        super().__init__(hass, db_manager)
        _LOGGER.debug("DataValidator initialized with DatabaseManager")

    async def validate_prediction_data(self, data: Dict[str, Any]) -> bool:
        """Validate prediction data structure. @zara

        Args:
            data: Prediction data dictionary

        Returns:
            True if valid, False otherwise
        """
        required_fields = ["timestamp", "prediction_kwh", "confidence"]
        for field in required_fields:
            if field not in data:
                _LOGGER.error("Missing required field in prediction data: %s", field)
                return False
        if not isinstance(data.get("prediction_kwh"), (int, float)):
            _LOGGER.error("Invalid type for prediction_kwh")
            return False
        if not isinstance(data.get("confidence"), (int, float)):
            _LOGGER.error("Invalid type for confidence")
            return False
        if data["prediction_kwh"] < 0:
            _LOGGER.error("Negative prediction value")
            return False
        if not 0 <= data["confidence"] <= 1:
            _LOGGER.error("Confidence out of range [0, 1]")
            return False
        return True

    async def validate_sample_data(self, sample: Dict[str, Any]) -> bool:
        """Validate ML sample data structure. @zara

        Args:
            sample: Sample data dictionary

        Returns:
            True if valid, False otherwise
        """
        required_fields = ["timestamp", "actual_power", "features"]
        for field in required_fields:
            if field not in sample:
                _LOGGER.error("Missing required field in sample: %s", field)
                return False
        features = sample.get("features", {})
        if not isinstance(features, dict):
            _LOGGER.error("Features must be a dictionary")
            return False
        recommended_features = ["hour", "temperature", "cloud_cover"]
        for feature in recommended_features:
            if feature not in features:
                _LOGGER.warning("Missing recommended feature: %s", feature)
        return True

    async def validate_model_state(self, state: Dict[str, Any]) -> bool:
        """Validate model state data. @zara

        Args:
            state: Model state dictionary

        Returns:
            True if valid, False otherwise
        """
        required_fields = ["version", "model_loaded", "training_samples"]
        for field in required_fields:
            if field not in state:
                _LOGGER.error("Missing required field in model state: %s", field)
                return False
        if state.get("version") != DATA_VERSION:
            _LOGGER.warning(
                "Model state version mismatch: %s != %s",
                state.get("version"),
                DATA_VERSION,
            )
        training_samples = state.get("training_samples", 0)
        if training_samples < 0:
            _LOGGER.error("Negative training sample count")
            return False
        return True

    async def validate_daily_forecast(self, forecast: Dict[str, Any]) -> bool:
        """Validate daily forecast data. @zara

        Args:
            forecast: Forecast data dictionary

        Returns:
            True if valid, False otherwise
        """
        required_fields = ["date", "prediction_kwh"]
        for field in required_fields:
            if field not in forecast:
                _LOGGER.error("Missing required field in forecast: %s", field)
                return False
        try:
            if forecast.get("date"):
                datetime.fromisoformat(forecast["date"])
        except (ValueError, TypeError):
            _LOGGER.error("Invalid date format: %s", forecast.get("date"))
            return False
        prediction_kwh = forecast.get("prediction_kwh")
        if prediction_kwh is not None and (
            not isinstance(prediction_kwh, (int, float)) or prediction_kwh < 0
        ):
            _LOGGER.error("Invalid prediction value: %s", prediction_kwh)
            return False
        return True

    async def check_data_quality(self) -> Dict[str, Any]:
        """Check overall data quality metrics from database. @zara

        Returns:
            Dictionary with quality metrics
        """
        try:
            total_row = await self.fetch_one("SELECT COUNT(*) FROM hourly_predictions")
            total_samples = total_row[0] if total_row else 0
            if total_samples == 0:
                return {
                    "total_samples": 0,
                    "valid_samples": 0,
                    "quality_score": 0.0,
                    "issues": ["No samples available"],
                }
            valid_row = await self.fetch_one(
                "SELECT COUNT(*) FROM hourly_predictions WHERE actual_kwh IS NOT NULL"
            )
            valid_samples = valid_row[0] if valid_row else 0
            quality_score = valid_samples / total_samples if total_samples > 0 else 0.0
            issues = []
            if total_samples < MIN_TRAINING_DATA_POINTS:
                issues.append(
                    f"Insufficient samples: {total_samples} < {MIN_TRAINING_DATA_POINTS}"
                )
            if quality_score < 0.9:
                issues.append(f"Low quality score: {quality_score:.2%}")
            return {
                "total_samples": total_samples,
                "valid_samples": valid_samples,
                "quality_score": quality_score,
                "sufficient_for_training": total_samples >= MIN_TRAINING_DATA_POINTS
                and quality_score >= 0.8,
                "issues": issues,
            }
        except Exception as error:
            _LOGGER.error("Failed to check data quality: %s", error)
            return {
                "total_samples": 0,
                "valid_samples": 0,
                "quality_score": 0.0,
                "issues": [f"Error: {str(error)}"],
            }

    async def validate_weather_forecast_in_db(
        self, forecast_date: str, hour: int
    ) -> Dict[str, Any]:
        """Validate weather forecast data in database. @zara

        Args:
            forecast_date: Date string (YYYY-MM-DD)
            hour: Hour (0-23)

        Returns:
            Validation result dictionary
        """
        result = {"valid": False, "exists": False, "issues": []}
        try:
            row = await self.fetch_one(
                "SELECT temperature, clouds, solar_radiation_wm2, humidity\n                   FROM weather_forecast\n                   WHERE forecast_date = ? AND hour = ?",
                (forecast_date, hour),
            )
            if not row:
                result["issues"].append("Weather forecast not found in database")
                return result
            result["exists"] = True
            temperature = row[0]
            clouds = row[1]
            solar_radiation = row[2]
            humidity = row[3]
            if temperature is not None:
                if not -50 <= temperature <= 60:
                    result["issues"].append(f"Temperature out of range: {temperature}")
            if clouds is not None:
                if not 0 <= clouds <= 100:
                    result["issues"].append(f"Cloud cover out of range: {clouds}")
            if humidity is not None:
                if not 0 <= humidity <= 100:
                    result["issues"].append(f"Humidity out of range: {humidity}")
            if solar_radiation is not None and solar_radiation < 0:
                result["issues"].append(f"Negative solar radiation: {solar_radiation}")
            result["valid"] = len(result["issues"]) == 0
            return result
        except Exception as error:
            _LOGGER.error("Failed to validate weather forecast: %s", error)
            result["issues"].append(f"Validation error: {str(error)}")
            return result

    async def validate_learning_config_in_db(self) -> Dict[str, Any]:
        """Validate physics learning config in database. @zara

        Returns:
            Validation result dictionary
        """
        result = {"valid": False, "exists": False, "issues": []}
        try:
            row = await self.fetch_one(
                "SELECT version, albedo, system_efficiency,\n                          learned_efficiency_factor, rolling_window_days\n                   FROM physics_learning_config WHERE id = 1"
            )
            if not row:
                result["issues"].append("Learning config not found in database")
                return result
            result["exists"] = True
            albedo = row[1]
            system_efficiency = row[2]
            if albedo is not None:
                if not 0.0 <= albedo <= 1.0:
                    result["issues"].append(f"Albedo out of range [0,1]: {albedo}")
            if system_efficiency is not None:
                if not 0.0 <= system_efficiency <= 1.0:
                    result["issues"].append(
                        f"System efficiency out of range [0,1]: {system_efficiency}"
                    )
            result["valid"] = len(result["issues"]) == 0
            result["config"] = {
                "version": row[0],
                "albedo": albedo,
                "system_efficiency": system_efficiency,
                "learned_efficiency_factor": row[3],
                "rolling_window_days": row[4],
            }
            return result
        except Exception as error:
            _LOGGER.error("Failed to validate learning config: %s", error)
            result["issues"].append(f"Validation error: {str(error)}")
            return result

    async def validate_model_weights_in_db(self) -> Dict[str, Any]:
        """Validate AI model weights in database. @zara

        Returns:
            Validation result with model info
        """
        result = {
            "exists": False,
            "valid": False,
            "has_attention": False,
            "issues": [],
            "model_info": {},
        }
        try:
            weights = await self.db.get_model_weights()
            if not weights:
                result["issues"].append("No model weights found in database")
                return result
            result["exists"] = True
            lstm_fields = ["Wf", "Wi", "Wc", "Wo", "bf", "bi", "bc", "bo"]
            output_fields = ["Wy", "by"]
            architecture_fields = ["input_size", "hidden_size"]
            for field in lstm_fields:
                if field not in weights:
                    result["issues"].append(f"Missing required LSTM field: {field}")
            for field in output_fields:
                if field not in weights:
                    result["issues"].append(f"Missing required output field: {field}")
            for field in architecture_fields:
                if field not in weights:
                    result["issues"].append(f"Missing architecture info: {field}")
            result["model_info"] = {
                "input_size": weights.get("input_size"),
                "hidden_size": weights.get("hidden_size"),
                "sequence_length": weights.get("sequence_length", 24),
                "num_outputs": weights.get("num_outputs", 1),
                "training_samples": weights.get("training_samples", 0),
                "accuracy": weights.get("accuracy"),
                "rmse": weights.get("rmse"),
                "last_trained": weights.get("last_trained"),
            }
            has_attention = weights.get("has_attention", False)
            result["has_attention"] = has_attention
            result["model_info"]["has_attention"] = has_attention
            if has_attention:
                attention_fields = [
                    "W_query",
                    "W_key",
                    "W_value",
                    "W_attn_out",
                    "b_attn_out",
                ]
                for field in attention_fields:
                    if field not in weights:
                        result["issues"].append(f"Missing attention field: {field}")
            for field in lstm_fields + output_fields:
                if field in weights:
                    weight_values = weights[field]
                    if isinstance(weight_values, list) and len(weight_values) == 0:
                        result["issues"].append(f"Empty weight array: {field}")
            result["valid"] = len(result["issues"]) == 0
            return result
        except Exception as error:
            _LOGGER.error("Failed to validate model weights: %s", error)
            result["issues"].append(f"Validation error: {str(error)}")
            return result

    async def validate_daily_summary(self, date_str: str) -> Dict[str, Any]:
        """Validate daily summary data in database. @zara

        Args:
            date_str: Date string (YYYY-MM-DD)

        Returns:
            Validation result dictionary
        """
        result = {"valid": False, "exists": False, "issues": []}
        try:
            row = await self.fetch_one(
                "SELECT predicted_total_kwh, actual_total_kwh,\n                          accuracy_percent, production_hours\n                   FROM daily_summaries WHERE date = ?",
                (date_str,),
            )
            if not row:
                result["issues"].append(f"Daily summary not found for {date_str}")
                return result
            result["exists"] = True
            predicted_kwh = row[0]
            actual_kwh = row[1]
            accuracy_percent = row[2]
            production_hours = row[3]
            if predicted_kwh is not None and predicted_kwh < 0:
                result["issues"].append(f"Negative predicted value: {predicted_kwh}")
            if actual_kwh is not None and actual_kwh < 0:
                result["issues"].append(f"Negative actual value: {actual_kwh}")
            if accuracy_percent is not None:
                if not -100 <= accuracy_percent <= 200:
                    result["issues"].append(
                        f"Accuracy out of range: {accuracy_percent}"
                    )
            if production_hours is not None:
                if not 0 <= production_hours <= 24:
                    result["issues"].append(
                        f"Invalid production hours: {production_hours}"
                    )
            result["valid"] = len(result["issues"]) == 0
            result["summary"] = {
                "predicted_total_kwh": predicted_kwh,
                "actual_total_kwh": actual_kwh,
                "accuracy_percent": accuracy_percent,
                "production_hours": production_hours,
            }
            return result
        except Exception as error:
            _LOGGER.error("Failed to validate daily summary: %s", error)
            result["issues"].append(f"Validation error: {str(error)}")
            return result

    async def get_validation_report(self) -> Dict[str, Any]:
        """Generate a comprehensive validation report. @zara

        Returns:
            Dictionary with validation report for all data types
        """
        report = {
            "timestamp": SafeDateTimeUtil.now().isoformat(),
            "overall_valid": True,
            "checks": {},
        }
        data_quality = await self.check_data_quality()
        report["checks"]["data_quality"] = data_quality
        if not data_quality.get("sufficient_for_training", False):
            report["overall_valid"] = False
        learning_config = await self.validate_learning_config_in_db()
        report["checks"]["learning_config"] = learning_config
        if not learning_config.get("valid", False):
            if learning_config.get("exists", False):
                report["overall_valid"] = False
        model_weights = await self.validate_model_weights_in_db()
        report["checks"]["model_weights"] = model_weights
        if not model_weights.get("valid", False):
            if model_weights.get("exists", False):
                report["overall_valid"] = False
        return report
