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

    async def validate_and_migrate_all(self) -> bool:
        """Validate all database tables and ensure schema integrity. @zara

        This method:
        1. Verifies all required tables exist
        2. Checks essential columns are present
        3. Initializes singleton config tables with defaults if empty
        4. Logs all validation actions

        Returns:
            True if all validations pass, False otherwise
        """
        try:
            _LOGGER.info("=== Database Schema Validation Starting ===")
            _var_var_0 = True
            for _var_var_1, _var_var_2 in REQUIRED_TABLES.items():
                _var_var_3 = await self._validate_table(_var_var_1, _var_var_2)
                if not _var_var_3:
                    _var_var_0 = False
                    self._log(f"Table validation failed: {_var_var_1}")
            await self._ensure_physics_learning_config()
            await self._ensure_visibility_learning()
            await self._ensure_coordinator_state()
            if self.migration_log:
                _LOGGER.info("=== Schema Validation Summary ===")
                for _var_var_4 in self.migration_log:
                    _LOGGER.info("  %s", _var_var_4)
            else:
                _LOGGER.info("All database tables valid - no changes needed")
            if self.healed_tables:
                _LOGGER.info(
                    "Initialized %d table(s): %s",
                    len(self.healed_tables),
                    ", ".join(self.healed_tables),
                )
            _LOGGER.info("=== Database Schema Validation Complete ===")
            return _var_var_0
        except Exception as _var_var_5:
            _LOGGER.error("Schema validation failed: %s", _var_var_5, exc_info=True)
            return False

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
