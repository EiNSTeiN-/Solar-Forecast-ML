# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
WeatherKalmanTracker: Online Kalman filter for intraday solar bias correction.

Tracks systematic bias between forecast and actual solar radiation within a day.
Resets daily. Adapts Q/R parameters based on observed residual statistics.

@zara
"""
import logging
from typing import Any, Dict, List
_LOGGER = logging.getLogger(__name__)

class WeatherKalmanTracker:
    """1D Kalman filter for solar radiation bias estimation. @zara

State model:
    x = bias (W/m²), random walk process
    z = actual_radiation - P0  (x-free GHI before terminal Kalman)

Adaptive parameters:
    Q adapts to hour-to-hour residual variability (volatile → higher Q)
    R adapts to residual spread (noisy → higher R)
"""
    MAX_CORRECTION = 200.0
    MAX_R_SCALE = 5.0
    Q_MIN = 2.0
    Q_MAX = 20.0
    R_MIN = 20.0
    R_MAX = 200.0
    ADAPT_ALPHA = 0.3

    def __init__(self, process_noise: float=5.0, measurement_noise: float=50.0):
        """Initialize Kalman tracker. @zara"""
        self.Q = process_noise
        self.R = measurement_noise
        self.x = 0.0
        self.P = 100.0
        self._updates_today = 0
        self._residuals_today = []
        _LOGGER.debug('WeatherKalmanTracker initialized: Q=%.1f, R=%.1f', self.Q, self.R)

    def predict(self) -> None:
        """Predict step - increase uncertainty. @zara"""
        self.P = self.P + self.Q

    def update(self, measurement_residual: float) -> None:
        """Update step with innovation-based R scaling. @zara"""
        self.predict()
        innovation = measurement_residual - self.x
        innovation_variance = self.P + self.R
        innovation_scale = max(1.0, innovation ** 2 / innovation_variance)
        effective_measurement_noise = self.R * min(innovation_scale, self.MAX_R_SCALE)
        kalman_gain = self.P / (self.P + effective_measurement_noise)
        self.x = self.x + kalman_gain * innovation
        self.P = (1.0 - kalman_gain) * self.P
        self._updates_today += 1
        self._residuals_today.append(measurement_residual)
        _LOGGER.debug('Kalman update: residual=%.1f, bias=%.1f, K=%.3f, P=%.1f, R_eff=%.1f, updates=%d', measurement_residual, self.x, kalman_gain, self.P, effective_measurement_noise, self._updates_today)

    def _adapt_parameters(self) -> None:
        """Adapt Q and R via EMA based on today's residual statistics. @zara"""
        if len(self._residuals_today) < 3:
            return
        residuals = self._residuals_today
        differences = [residuals[index] - residuals[index - 1] for index in range(1, len(residuals))]
        difference_variance = sum((difference ** 2 for difference in differences)) / len(differences)
        target_process_noise = max(self.Q_MIN, min(self.Q_MAX, difference_variance / 200.0))
        mean_residual = sum(residuals) / len(residuals)
        residual_variance = sum(((residual - mean_residual) ** 2 for residual in residuals)) / len(residuals)
        target_measurement_noise = max(self.R_MIN, min(self.R_MAX, residual_variance / 25.0))
        previous_process_noise, previous_measurement_noise = (self.Q, self.R)
        self.Q = max(self.Q_MIN, min(self.Q_MAX, self.Q * (1.0 - self.ADAPT_ALPHA) + target_process_noise * self.ADAPT_ALPHA))
        self.R = max(self.R_MIN, min(self.R_MAX, self.R * (1.0 - self.ADAPT_ALPHA) + target_measurement_noise * self.ADAPT_ALPHA))
        _LOGGER.info('Kalman adaptive: Q=%.1f->%.1f, R=%.1f->%.1f (diff_var=%.0f, res_var=%.0f, %d samples)', previous_process_noise, self.Q, previous_measurement_noise, self.R, difference_variance, residual_variance, len(residuals))

    def get_correction(self) -> float:
        """Get current bias correction, clamped. @zara"""
        if self._updates_today == 0:
            return 0.0
        return max(-self.MAX_CORRECTION, min(self.MAX_CORRECTION, self.x))

    def reset(self) -> None:
        """Daily reset with parameter adaptation. @zara"""
        self._adapt_parameters()
        if self._updates_today > 0:
            _LOGGER.info('Kalman daily reset: final bias=%.1f, updates=%d, next Q=%.1f, R=%.1f', self.x, self._updates_today, self.Q, self.R)
        self.x = 0.0
        self.P = 100.0
        self._updates_today = 0
        self._residuals_today.clear()

    def get_state(self) -> Dict[str, Any]:
        """Get current state for diagnostics. @zara"""
        return {'bias': round(self.x, 1), 'uncertainty': round(self.P, 1), 'updates_today': self._updates_today, 'correction': round(self.get_correction(), 1), 'confidence': round(self.get_confidence(), 3), 'Q': round(self.Q, 1), 'R': round(self.R, 1)}

    def get_confidence(self) -> float:
        """Confidence based on uncertainty reduction. @zara"""
        confidence = 1.0 - self.P / 100.0
        return max(0.0, min(1.0, confidence))
