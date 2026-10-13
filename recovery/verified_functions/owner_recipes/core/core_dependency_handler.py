# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import logging
from typing import Any, Dict


class DependencyHandler:

    def _get_package_version_sync(self, package: str) -> str:
        """Blocking function to get the package version. @zara"""
        try:
            from importlib.metadata import version as _var_var_7
        except ImportError:
            try:
                from importlib_metadata import version as _var_var_7
            except ImportError:
                _LOGGER.warning(
                    "Could not import 'importlib.metadata' or 'importlib_metadata'"
                )
                return "unknown (import error)"
        try:
            return _var_var_7(package)
        except Exception:
            if package == "numpy":
                try:
                    import numpy as _var_var_0

                    return _var_var_0.__version__
                except Exception as _var_var_3:
                    _LOGGER.debug(f"Could not get numpy version: {_var_var_3}")
            return "unknown"
