# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
# fmt: off
"""Runtime context for STATS API views.

Read and bind these names only as module attributes (attribute access only).
Never ``from .context import HASS`` — that copies the initial None.
"""
# fmt: on
from __future__ import annotations
from pathlib import Path
from typing import Any

HASS: Any = None
SOLAR_PATH: Path | None = None
GRID_PATH: Path | None = None
