# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import json
import logging
import hashlib
import math
import uuid
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo


class EnsembleShadowHandler:

    @classmethod
    def select_canonical_batch_rows(
        cls, rows: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Keep one pre-sunrise scheduled batch row per target date/hour."""
        _var_var_47 = {}
        for _var_var_48 in rows:
            _var_var_49 = str(_var_var_48.get("forecast_cycle") or "")
            _var_var_50 = str(_var_var_48.get("trigger_kind") or "")
            if (
                _var_var_49 != CANONICAL_SHADOW_FORECAST_CYCLE
                or _var_var_50 != CANONICAL_SHADOW_TRIGGER_KIND
            ):
                continue
            _var_var_51 = str(_var_var_48.get("target_date") or "")
            try:
                _var_var_52 = int(_var_var_48.get("target_hour"))
            except (TypeError, ValueError):
                continue
            if not _var_var_51 or not 0 <= _var_var_52 <= 23:
                continue
            _var_var_53 = (_var_var_51, _var_var_52)
            _var_var_54 = dict(_var_var_48)
            _var_var_55 = _var_var_47.get(_var_var_53)
            if _var_var_55 is None:
                _var_var_47[_var_var_53] = _var_var_54
                continue
            _var_var_56 = str(_var_var_55.get("forecast_created_at") or "")
            _var_var_57 = str(_var_var_54.get("forecast_created_at") or "")
            _var_var_58 = str(_var_var_55.get("morning_batch_id") or "")
            _var_var_59 = str(_var_var_54.get("morning_batch_id") or "")
            if not (_var_var_57, _var_var_59) < (_var_var_56, _var_var_58):
                continue
            _var_var_47[_var_var_53] = _var_var_54
        return [_var_var_47[_var_var_53] for _var_var_53 in sorted(_var_var_47)]
