# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from datetime import datetime
from typing import Any, Dict, List
from homeassistant.core import HomeAssistant
from .db_manager import DatabaseManager
from .data_io import DataManagerIO


class DataSchemaValidator(DataManagerIO):

    async def validate_table_integrity(self, table_name: str) -> Dict[str, Any]:
        """Check integrity of a specific table. @zara

        Args:
            table_name: Name of table to check

        Returns:
            Dictionary with integrity check results
        """
        _var_var_11 = {
            "table": table_name,
            "exists": False,
            "row_count": 0,
            "columns": [],
            "indexes": [],
            "issues": [],
        }
        try:
            _var_var_6 = await self.fetch_one(
                "SELECT name FROM sqlite_master\n                   WHERE type='table' AND name=?",
                (table_name,),
            )
            if not _var_var_6:
                _var_var_11["issues"].append("Table does not exist")
                return _var_var_11
            _var_var_11["exists"] = True
            _var_var_12 = await self.fetch_one(f"SELECT COUNT(*) FROM {table_name}")
            _var_var_11["row_count"] = _var_var_12[0] if _var_var_12 else 0
            _var_var_7 = await self.fetch_all(f"PRAGMA table_info({table_name})")
            _var_var_11["columns"] = [
                {
                    "name": _var_var_8[1],
                    "type": _var_var_8[2],
                    "notnull": bool(_var_var_8[3]),
                    "default": _var_var_8[4],
                    "pk": bool(_var_var_8[5]),
                }
                for _var_var_8 in _var_var_7
            ]
            _var_var_13 = await self.fetch_all(f"PRAGMA index_list({table_name})")
            _var_var_11["indexes"] = [_var_var_14[1] for _var_var_14 in _var_var_13]
            return _var_var_11
        except Exception as _var_var_5:
            _LOGGER.error("Failed to check integrity of %s: %s", table_name, _var_var_5)
            _var_var_11["issues"].append(f"Error: {str(_var_var_5)}")
            return _var_var_11
