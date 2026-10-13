# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
from ..core.core_helpers import get_season, SafeDateTimeUtil
from ..core.core_panel_group_forecast import get_group_capacity_kwp
from ..data.data_learning_filter import (
    HOURLY_LEARNING_ELIGIBILITY_SQL,
    PANEL_GROUP_LEARNING_ELIGIBILITY_SQL,
)


class DriftMonitor:

    async def calculate_rolling_metrics(self, date_str: str) -> Dict[str, Any]:
        """Calculate rolling-window metrics for all scopes and windows. @zara V17.0.0

        Called by production_scheduled_tasks as EOD Step 14.

        Args:
            date_str: Date string YYYY-MM-DD

        Returns:
            Dict with calculation summary
        """
        await self.initialize()
        _var_var_40 = datetime.strptime(date_str, "%Y-%m-%d").month
        _var_var_41 = get_season(_var_var_40)
        _var_var_34 = self._get_scopes()
        _var_var_42 = 0
        _var_var_43 = 0
        _var_var_44 = []
        _var_var_45 = []
        _var_var_46 = {}
        for _var_var_29 in _var_var_34:
            for _var_var_47 in ROLLING_WINDOWS:
                for _var_var_48 in (None, _var_var_41):
                    try:
                        _var_var_49 = await self._compute_rolling_metrics(
                            _var_var_29, _var_var_47, date_str, _var_var_48
                        )
                        if _var_var_49:
                            if _var_var_49.get("sample_count", 0) >= 3:
                                await self._db.save_drift_rolling_metric(
                                    _var_var_29, _var_var_47, _var_var_48, _var_var_49
                                )
                                _var_var_46[_var_var_29, _var_var_47, _var_var_48] = (
                                    _var_var_49
                                )
                                _var_var_42 += 1
                    except Exception as _var_var_33:
                        _var_var_44.append(f"{_var_var_29}/{_var_var_47}/{_var_var_48}")
                        _LOGGER.debug(
                            "Rolling metric %s/%d/%s failed: %s",
                            _var_var_29,
                            _var_var_47,
                            _var_var_48,
                            _var_var_33,
                        )
                        continue
            try:
                _var_var_50 = await self._compute_bucket_metrics(
                    _var_var_29, date_str, _var_var_41
                )
                _var_var_43 += _var_var_50
            except Exception as _var_var_33:
                _var_var_45.append(f"bucket/{_var_var_29}")
                _LOGGER.error(
                    "Bucket metrics for %s failed: %s", _var_var_29, _var_var_33
                )
                continue
        try:
            _var_var_51 = await self._run_cusum_detection(date_str)
            _var_var_43 += _var_var_51
        except Exception as _var_var_33:
            _var_var_45.append("cusum")
            _LOGGER.error("CUSUM detection failed: %s", _var_var_33)
        if _var_var_44:
            _LOGGER.error(
                "Drift rolling metrics incomplete: %d calculations failed (%s)",
                len(_var_var_44),
                ", ".join(_var_var_44),
            )
        elif not _var_var_45:
            try:
                _var_var_52 = await self._check_and_respond_to_drift(
                    date_str, _var_var_46
                )
                _var_var_43 += _var_var_52
            except Exception as _var_var_33:
                _var_var_45.append("response")
                _LOGGER.error("Drift response check failed: %s", _var_var_33)
        _var_var_53 = 1.0
        _var_var_54 = False
        _var_var_55 = False
        if not _var_var_44 and (not _var_var_45):
            for _var_var_29 in _var_var_34:
                _var_var_56 = _var_var_46.get((_var_var_29, 7, None))
                _var_var_57 = _var_var_46.get((_var_var_29, 14, None))
                if not _var_var_56 or not _var_var_57:
                    continue
                try:
                    _var_var_58 = await self._compute_and_apply_bias_correction(
                        _var_var_29, date_str, _var_var_56, _var_var_57
                    )
                except Exception as _var_var_33:
                    _var_var_45.append(f"bias/{_var_var_29}")
                    _LOGGER.error(
                        "Bias correction for %s failed: %s", _var_var_29, _var_var_33
                    )
                    continue
                if _var_var_58 is None:
                    continue
                if _var_var_29 == "global":
                    _var_var_53 = _var_var_58
                    _var_var_54 = True
                    try:
                        _var_var_55 = await self._update_solar_clamp("global")
                    except Exception as _var_var_33:
                        _var_var_45.append("solar_clamp/global")
                        _LOGGER.error("Solar clamp update failed: %s", _var_var_33)
                        continue
            if not _var_var_45:
                _var_var_59 = []
                for _var_var_29 in _var_var_34:
                    _var_var_31 = self._cusum_detectors.get(_var_var_29)
                    if _var_var_31:
                        try:
                            await self._db.save_drift_cusum_state(
                                _var_var_29, _var_var_31.to_dict()
                            )
                        except Exception as _var_var_33:
                            _var_var_59.append(_var_var_29)
                            _LOGGER.error(
                                "Drift state persistence for %s failed: %s",
                                _var_var_29,
                                _var_var_33,
                            )
                            continue
                _var_var_45.extend(
                    (f"state/{_var_var_29}" for _var_var_29 in _var_var_59)
                )
        if _var_var_45:
            _LOGGER.error(
                "Drift adaptive operations incomplete: %d failed (%s)",
                len(_var_var_45),
                ", ".join(_var_var_45),
            )
        _LOGGER.info(
            "Drift detection complete: %d metrics saved, %d events created",
            _var_var_42,
            _var_var_43,
        )
        return {
            "metrics_saved": _var_var_42,
            "metrics_failed": len(_var_var_44),
            "operations_failed": len(_var_var_45),
            "events_created": _var_var_43,
            "scopes_checked": len(_var_var_34),
            "bias_correction_global": round(_var_var_53, 4),
            "global_bias_fresh": _var_var_54,
            "global_clamp_fresh": _var_var_55,
        }
