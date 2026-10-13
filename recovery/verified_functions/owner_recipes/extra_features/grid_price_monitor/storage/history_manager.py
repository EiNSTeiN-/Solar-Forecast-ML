# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING


class HistoryManager:

    async def async_load(self) -> bool:
        """Check if history data exists in database @zara

        Returns:
            True if history is ready
        """
        try:
            _var_var_0 = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM GPM_price_history WHERE timestamp LIKE '%+%' OR timestamp LIKE '%Z%'"
            )
            if _var_var_0 and _var_var_0["cnt"] > 0:
                _LOGGER.info(
                    "Starting database migration for GPM_price_history to naive local timestamps..."
                )
                _var_var_1 = await self._db.fetchall(
                    "SELECT timestamp, price_net, total_price, hour FROM GPM_price_history"
                )
                _var_var_2 = {}
                for _var_var_3 in _var_var_1:
                    _var_var_4 = _var_var_3["timestamp"]
                    try:
                        _var_var_5 = datetime.fromisoformat(
                            _var_var_4.replace("Z", "+00:00")
                        )
                        if _var_var_5.tzinfo is not None:
                            _var_var_6 = _var_var_5.astimezone()
                            _var_var_7 = _var_var_6.replace(tzinfo=None)
                        else:
                            _var_var_7 = _var_var_5
                        _var_var_8 = _var_var_7.isoformat()
                        _var_var_9 = _var_var_3["price_net"]
                        _var_var_10 = _var_var_3["total_price"]
                        _var_var_11 = _var_var_3["hour"]
                        if _var_var_8 in _var_var_2:
                            _var_var_12 = _var_var_2[_var_var_8]
                            if _var_var_12["total_price"] is None:
                                if _var_var_10 is not None:
                                    _var_var_2[_var_var_8] = {
                                        "price_net": _var_var_9,
                                        "total_price": _var_var_10,
                                        "hour": _var_var_11,
                                    }
                                    continue
                                continue
                            continue
                        _var_var_2[_var_var_8] = {
                            "price_net": _var_var_9,
                            "total_price": _var_var_10,
                            "hour": _var_var_11,
                        }
                    except Exception as _var_var_13:
                        _LOGGER.warning(
                            "Migration skipping invalid timestamp %s: %s",
                            _var_var_4,
                            _var_var_13,
                        )
                        continue
                await self._db.execute(
                    "DELETE FROM GPM_price_history", auto_commit=False
                )
                _var_var_15 = [
                    (
                        _var_var_8,
                        _var_var_14["price_net"],
                        _var_var_14["total_price"],
                        _var_var_14["hour"],
                    )
                    for _var_var_8, _var_var_14 in _var_var_2.items()
                ]
                await self._db.executemany(
                    "INSERT INTO GPM_price_history (timestamp, price_net, total_price, hour)\n                       VALUES (?, ?, ?, ?)",
                    _var_var_15,
                )
                await self._db.commit()
                _LOGGER.info(
                    "Successfully migrated %d price history entries to naive local timestamps",
                    len(_var_var_15),
                )
            _var_var_0 = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM GPM_price_history"
            )
            _var_var_16 = _var_var_0["cnt"] if _var_var_0 else 0
            if _var_var_16 > 0:
                self._loaded = True
                _LOGGER.info("Loaded price history with %d entries", _var_var_16)
            await self._async_cleanup_old_entries()
            return True
        except Exception as _var_var_17:
            _LOGGER.warning("Failed to load price history: %s", _var_var_17)
            return True
