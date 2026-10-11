# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
# fmt: off
"""AI Module for Weather Fusion AI.

This module contains the core AI/ML components for weather learning:

- PrecisionTracker: Tracks forecast vs actual deviations per bucket
- WeatherLearner: Learns correction factors from historical data
- PatternDetector: Detects weather patterns and anomalies
- ForecastCorrector: Applies learned corrections to forecasts

Architecture:

    ┌─────────────────────────────────────────────────────────────────┐
    │                    Weather Fusion AI Core                       │
    ├─────────────────────────────────────────────────────────────────┤
    │                                                                 │
    │  [Forecast Sources] ──► [Expert Blender] ──► Raw Forecast       │
    │                                                   │             │
    │                                                   ▼             │
    │  [Local Sensors] ──► [Actual Tracker] ──► [Precision Tracker]   │
    │                                                   │             │
    │                                                   ▼             │
    │                                          [Weather Learner]      │
    │                                          ┌───────┴───────┐      │
    │                                          │   Buckets:    │      │
    │                                          │ • Hour (0-23) │      │
    │                                          │ • CloudType   │      │
    │                                          │ • Season      │      │
    │                                          │ • Field       │      │
    │                                          └───────┬───────┘      │
    │                                                  │              │
    │                                                  ▼              │
    │                                         [Pattern Detector]      │
    │                                                  │              │
    │                                                  ▼              │
    │  Raw Forecast ──────────────────────► [Forecast Corrector]      │
    │                                                  │              │
    │                                                  ▼              │
    │                                         Corrected Forecast      │
    │                                                                 │
    └─────────────────────────────────────────────────────────────────┘

Data Storage Structure (/config/weather_fusion_ai/):

    weather_fusion_ai/
    ├── learning/
    │   ├── hourly_buckets.json      # Correction factors per hour (0-23)
    │   ├── cloud_type_buckets.json  # Correction factors per cloud type
    │   ├── seasonal_buckets.json    # Seasonal correction factors
    │   ├── field_buckets.json       # Per-field correction factors
    │   └── expert_weights.json      # Learned expert weights
    │
    ├── history/
    │   ├── actual/                  # Actual weather from sensors
    │   │   ├── 2025-01.json
    │   │   └── ...
    │   ├── forecast/                # Historical forecasts
    │   │   ├── 2025-01.json
    │   │   └── ...
    │   └── deviations/              # Forecast vs Actual deviations
    │       ├── 2025-01.json
    │       └── ...
    │
    ├── cache/
    │   ├── open_meteo.json          # API response cache
    │   ├── bright_sky.json
    │   ├── wttr_in.json
    │   └── pirate_weather.json
    │
    ├── models/
    │   ├── pattern_weights.json     # Pattern detection weights
    │   └── anomaly_thresholds.json  # Anomaly detection config
    │
    └── logs/
        └── learning_log.json        # Learning history & events

@zara
"""
# fmt: on
from .precision_tracker import PrecisionTracker
from .weather_learner import WeatherLearner
from .pattern_detector import PatternDetector
from .forecast_corrector import ForecastCorrector

__all__ = ["PrecisionTracker", "WeatherLearner", "PatternDetector", "ForecastCorrector"]
