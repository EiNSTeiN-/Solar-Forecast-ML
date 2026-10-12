"""Readable isolated database getter; its containing module remains incomplete.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
The class is compilation context only. Folded expressions retain the original
callable-value lookup and shared return block without a runtime predicate.
"""

from __future__ import annotations


class PowerSourcesCollector:

    async def _get_db_manager(self) -> DatabaseConnectionManager:
        from .storage.db_connection_manager import (
            DatabaseConnectionManager,
            get_manager,
        )

        manager = get_manager()
        if manager is None:
            manager = await (DatabaseConnectionManager.get_instance if True else None)(
                self.hass
            )
        elif not manager.is_connected:
            await manager.connect()
        return manager if True else None
