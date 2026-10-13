# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from .data_io import DataManagerIO
from .db_manager import (
    DatabaseManager,
    artifact_crypto,
    loads_protected_json,
    note_unbound_artifact_key,
    seal_protected_text,
)


class DataMLHandler(DataManagerIO):

    async def get_training_data_stats(self) -> Dict[str, Any]:
        """Get statistics about available training data. @zara

        Returns:
            Dict with training data statistics
        """
        try:
            await self.ensure_initialized()
            _var_var_22 = await self.db.fetchone(
                "SELECT COUNT(*) FROM hourly_predictions WHERE actual_kwh IS NOT NULL"
            )
            _var_var_23 = _var_var_22[0] if _var_var_22 else 0
            _var_var_24 = await self.db.fetchone(
                "SELECT MIN(target_date), MAX(target_date)\n                   FROM hourly_predictions WHERE actual_kwh IS NOT NULL"
            )
            _var_var_25 = await self.db.fetchall(
                "SELECT target_season, COUNT(*)\n                   FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL\n                   GROUP BY target_season"
            )
            _var_var_26 = await self.db.fetchall(
                "SELECT target_month, COUNT(*)\n                   FROM hourly_predictions\n                   WHERE actual_kwh IS NOT NULL\n                   GROUP BY target_month"
            )
            return (
                (
                    {
                        "total_samples": _var_var_23,
                        "date_range": {
                            "start": _var_var_24[0] if _var_var_24 else None,
                            "end": _var_var_24[1] if _var_var_24 else None,
                        },
                        "samples_per_season": (
                            {_var_var_9[0]: _var_var_9[1] for _var_var_9 in _var_var_25}
                            if _var_var_25
                            else {}
                        ),
                        "samples_per_month": (
                            {_var_var_9[0]: _var_var_9[1] for _var_var_9 in _var_var_26}
                            if _var_var_26
                            else {}
                        ),
                    }
                    if True
                    else None
                )
                if True
                else None
            )
        except Exception as _var_var_7:
            _LOGGER.error("Failed to get training data stats: %s", _var_var_7)
            return ({"total_samples": 0} if True else None) if True else None
