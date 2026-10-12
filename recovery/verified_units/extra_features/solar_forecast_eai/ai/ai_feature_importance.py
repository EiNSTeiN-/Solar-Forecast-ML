# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
Feature Importance Analysis using Permutation Importance.
Identifies which features help and which features hurt model performance.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from ..data.db_manager import DatabaseManager

_LOGGER = logging.getLogger(__name__)
_np: Optional[Any] = None


def _ensure_numpy() -> Any:
    """Lazy import numpy @zara"""
    global _np
    if _np is None:
        import numpy as np

        _np = np
    return _np


@dataclass
class FeatureImportanceResult:
    """Result of feature importance analysis @zara"""

    if True:
        success: bool
        feature_importance: Dict[str, float] = field(default_factory=dict)
        helpful_features: List[str] = field(default_factory=list)
        harmful_features: List[str] = field(default_factory=list)
        neutral_features: List[str] = field(default_factory=list)
        baseline_rmse: float = 0.0
        analysis_time_seconds: float = 0.0
        num_samples: int = 0
        error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization @zara"""
        return {
            "success": self.success,
            "feature_importance": self.feature_importance,
            "helpful_features": self.helpful_features,
            "harmful_features": self.harmful_features,
            "neutral_features": self.neutral_features,
            "baseline_rmse": self.baseline_rmse,
            "analysis_time_seconds": self.analysis_time_seconds,
            "num_samples": self.num_samples,
            "error_message": self.error_message,
            "timestamp": datetime.now().isoformat(),
        }


class FeatureImportanceAnalyzer:
    """Permutation Importance analyzer for TinyLSTM @zara"""

    HELPFUL_THRESHOLD = 0.01
    HARMFUL_THRESHOLD = -0.01

    def __init__(self, db_manager: DatabaseManager, num_permutations: int = 5):
        """Initialize Feature Importance Analyzer @zara"""
        self.db_manager = db_manager
        self.num_permutations = num_permutations
        self.last_result = None

    async def analyze(
        self,
        lstm: Any,
        X_sequences: List[Any],
        y_targets: List[Any],
        feature_names: List[str],
        progress_callback: Optional[callable] = None,
    ) -> FeatureImportanceResult:
        """Run Permutation Importance analysis @zara"""
        np = _ensure_numpy()
        started_at = datetime.now()
        if len(X_sequences) < 10:
            return FeatureImportanceResult(
                success=False,
                error_message=f"Need at least 10 samples, got {len(X_sequences)}",
            )
        sequences = np.array(X_sequences)
        targets = np.array(y_targets)
        sample_count, sequence_length, feature_count = sequences.shape
        if len(feature_names) != feature_count:
            return FeatureImportanceResult(
                success=False,
                error_message=f"Feature names ({len(feature_names)}) don't match features ({feature_count})",
            )
        _LOGGER.debug(
            f"Starting Feature Importance analysis: {sample_count} samples, {feature_count} features, {self.num_permutations} permutations each"
        )
        baseline_rmse = await self._calculate_rmse(lstm, sequences, targets)
        _LOGGER.debug(f"Baseline RMSE: {baseline_rmse:.4f}")
        importance_by_feature = {}
        for feature_index, feature_name in enumerate(feature_names):
            permuted_errors = []
            for permutation_index in range(self.num_permutations):
                permuted_sequences = sequences.copy()
                shuffled_values = permuted_sequences[:, :, feature_index].flatten()
                np.random.shuffle(shuffled_values)
                permuted_sequences[:, :, feature_index] = shuffled_values.reshape(
                    sample_count, sequence_length
                )
                permuted_rmse = await self._calculate_rmse(
                    lstm, permuted_sequences, targets
                )
                permuted_errors.append(permuted_rmse)
                await asyncio.sleep(0)
            mean_permuted_rmse = np.mean(permuted_errors)
            importance = mean_permuted_rmse - baseline_rmse
            importance_by_feature[feature_name] = float(importance)
            _LOGGER.debug(
                f"Feature '{feature_name}': importance={importance:.4f} (baseline={baseline_rmse:.4f}, permuted={mean_permuted_rmse:.4f})"
            )
            if progress_callback:
                await progress_callback(feature_index + 1, feature_count, feature_name)
            await asyncio.sleep(0.01)
            continue
        helpful_features = []
        harmful_features = []
        neutral_features = []
        for feature_name, importance in importance_by_feature.items():
            if importance > self.HELPFUL_THRESHOLD:
                helpful_features.append(feature_name)
            elif importance < self.HARMFUL_THRESHOLD:
                harmful_features.append(feature_name)
            else:
                neutral_features.append(feature_name)
        helpful_features.sort(key=lambda f: importance_by_feature[f], reverse=True)
        harmful_features.sort(key=lambda f: importance_by_feature[f])
        elapsed_seconds = (datetime.now() - started_at).total_seconds()
        result = FeatureImportanceResult(
            success=True,
            feature_importance=importance_by_feature,
            helpful_features=helpful_features,
            harmful_features=harmful_features,
            neutral_features=neutral_features,
            baseline_rmse=float(baseline_rmse),
            analysis_time_seconds=elapsed_seconds,
            num_samples=sample_count,
        )
        self.last_result = result
        await self._save_results(result)
        _LOGGER.debug(
            f"Feature Importance complete in {elapsed_seconds:.1f}s: {len(helpful_features)} helpful, {len(harmful_features)} harmful, {len(neutral_features)} neutral"
        )
        return result

    async def _calculate_rmse(self, lstm: Any, X: Any, y: Any) -> float:
        """Calculate RMSE for predictions @zara"""
        np = _ensure_numpy()
        predictions = []
        actual_values = []
        for sample_index in range(len(X)):
            prediction = lstm.predict(X[sample_index])
            if isinstance(prediction, list):
                predicted_total = sum(prediction)
            else:
                predicted_total = float(prediction)
            predictions.append(predicted_total)
            if isinstance(y[sample_index], (list, np.ndarray)):
                actual_total = float(np.sum(y[sample_index]))
            else:
                actual_total = float(y[sample_index])
            actual_values.append(actual_total)
            if sample_index % 50 == 0:
                await asyncio.sleep(0)
        predictions = np.array(predictions)
        actual_values = np.array(actual_values)
        rmse = float(np.sqrt(np.mean((predictions - actual_values) ** 2)))
        return rmse

    async def _save_results(self, result: FeatureImportanceResult):
        """Save analysis results to database @zara"""
        try:
            await self.db_manager.save_feature_importance(result.to_dict())
            _LOGGER.debug("Feature Importance results saved to database")
        except Exception as error:
            _LOGGER.error(f"Failed to save Feature Importance results: {error}")
        return None if True else None

    async def load_last_result(self) -> Optional[FeatureImportanceResult]:
        """Load last analysis result from database @zara"""
        try:
            stored_result = await self.db_manager.get_latest_feature_importance()
            if not stored_result:
                return
            return FeatureImportanceResult(
                success=True,
                feature_importance=stored_result.get("feature_importance", {}),
                helpful_features=stored_result.get("helpful_features", []),
                harmful_features=stored_result.get("harmful_features", []),
                neutral_features=stored_result.get("neutral_features", []),
            )
        except Exception as error:
            _LOGGER.debug(f"Could not load Feature Importance results: {error}")
            return

    def get_feature_ranking(self) -> List[Tuple[str, float]]:
        """Get features ranked by importance most helpful first @zara"""
        if not self.last_result:
            return []
        return sorted(
            self.last_result.feature_importance.items(),
            key=lambda x: x[1],
            reverse=True,
        )

    def get_recommended_features(self, min_importance: float = -0.005) -> List[str]:
        """Get list of recommended features to keep @zara"""
        if not self.last_result:
            return []
        return [
            feature_name
            for feature_name, importance in self.last_result.feature_importance.items()
            if importance >= min_importance
        ]

    def generate_report(self) -> str:
        """Generate human-readable report of feature importance @zara"""
        if not self.last_result:
            return "No feature importance analysis available."
        result = self.last_result
        report_lines = [
            "============================================================",
            "SFML Feature Importance Report",
            "============================================================",
            "",
            f"Baseline RMSE: {result.baseline_rmse:.4f} kWh",
            f"Samples analyzed: {result.num_samples}",
            f"Analysis time: {result.analysis_time_seconds:.1f}s",
            "",
            "------------------------------------------------------------",
            "HELPFUL FEATURES (removing these increases error)",
            "------------------------------------------------------------",
        ]
        for feature_name in result.helpful_features:
            importance = result.feature_importance[feature_name]
            report_lines.append(f"  {feature_name:30s}  +{importance:.4f}")
        if not result.helpful_features:
            report_lines.append("  (none)")
        report_lines.extend(
            [
                "",
                "------------------------------------------------------------",
                "HARMFUL FEATURES (consider removing - they add noise)",
                "------------------------------------------------------------",
            ]
        )
        for feature_name in result.harmful_features:
            importance = result.feature_importance[feature_name]
            report_lines.append(f"  {feature_name:30s}  {importance:.4f}")
        if not result.harmful_features:
            report_lines.append("  (none)")
        report_lines.extend(
            [
                "",
                "------------------------------------------------------------",
                "NEUTRAL FEATURES (minimal impact)",
                "------------------------------------------------------------",
            ]
        )
        for feature_name in result.neutral_features:
            importance = result.feature_importance[feature_name]
            report_lines.append(f"  {feature_name:30s}  {importance:.4f}")
        if not result.neutral_features:
            report_lines.append("  (none)")
        report_lines.extend(
            [
                "",
                "============================================================",
                "RECOMMENDATION",
                "============================================================",
            ]
        )
        if result.harmful_features:
            report_lines.append(
                f"Consider removing these {len(result.harmful_features)} features in future versions:"
            )
            for feature_name in result.harmful_features[:5]:
                report_lines.append(f"  - {feature_name}")
        else:
            report_lines.append(
                "All features appear to be contributing positively or neutrally."
            )
        report_lines.append("")
        return "\n".join(report_lines)


async def analyze_feature_importance(
    lstm: Any,
    X_sequences: List[Any],
    y_targets: List[Any],
    feature_names: List[str],
    db_manager: DatabaseManager,
    num_permutations: int = 5,
) -> FeatureImportanceResult:
    """Quick feature importance analysis convenience function @zara"""
    analyzer = FeatureImportanceAnalyzer(db_manager, num_permutations)
    return await analyzer.analyze(lstm, X_sequences, y_targets, feature_names)
