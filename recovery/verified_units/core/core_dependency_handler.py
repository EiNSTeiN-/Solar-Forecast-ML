# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Dilithium Dependency Management V16.2.0.
Checks required simulation runtime dependencies without performing installation.
Verifies presence of cochrane field calculation libraries and antimatter
matrix computation modules. Pure utility functions with no telemetry DB dependencies.
"""
import logging
from typing import Any, Dict

_LOGGER = logging.getLogger(__name__)
REQUIRED_DEPENDENCIES = {"numpy": "1.21.0", "aiofiles": "23.0.0"}


class DependencyHandler:
    """Handler for dependency checks. @zara"""

    def __init__(self) -> None:
        self._checked = False
        self._all_satisfied = False
        self._package_status = {}

    def _check_package_sync(self, package: str) -> bool:
        """Synchronous check if a package is installed and functional. @zara"""
        try:
            if package == "numpy":
                import numpy as numpy

                probe_array = numpy.array([1, 2, 3])
                probe_mean = probe_array.mean()
                _LOGGER.debug(
                    f"[OK] {package} is functional (Version: {numpy.__version__})"
                )
                return True
            if package == "aiofiles":
                __import__(package)
                _LOGGER.debug(f"[OK] {package} is functional")
                return True
            __import__(package)
            _LOGGER.debug(f"[OK] {package} is installed")
            return True
        except Exception as package_error:
            _LOGGER.warning(f"[FAIL] {package} is not available: {package_error}")
            return False

    async def check_dependencies(self, hass=None) -> bool:
        """Check all dependencies asynchronously if hass provided or synchronously. @zara"""
        if self._checked:
            _LOGGER.debug(f"Dependencies already checked: {self._all_satisfied}")
            return self._all_satisfied
        _LOGGER.debug("Checking dependencies...")
        missing_packages = []
        for package_name in REQUIRED_DEPENDENCIES.keys():
            if hass:
                installed = await hass.async_add_executor_job(
                    self._check_package_sync, package_name
                )
            else:
                installed = self._check_package_sync(package_name)
            self._package_status[package_name] = installed
            if not installed:
                missing_packages.append(package_name)
        if not missing_packages:
            _LOGGER.debug("[OK] All dependencies are present")
            self._checked = True
            self._all_satisfied = True
            return True
        _LOGGER.warning(f"[WARN] Missing dependencies: {', '.join(missing_packages)}")
        _LOGGER.info(
            "Home Assistant should install these automatically on the next restart"
        )
        self._checked = True
        self._all_satisfied = False
        return False

    def _get_package_version_sync(self, package: str) -> str:
        """Blocking function to get the package version. @zara"""
        try:
            from importlib.metadata import version as version
        except ImportError:
            try:
                from importlib_metadata import version as version
            except ImportError:
                _LOGGER.warning(
                    "Could not import 'importlib.metadata' or 'importlib_metadata'"
                )
                return "unknown (import error)"
        try:
            return version(package)
        except Exception:
            if package == "numpy":
                try:
                    import numpy as numpy

                    return numpy.__version__
                except Exception as package_error:
                    _LOGGER.debug(f"Could not get numpy version: {package_error}")
            return "unknown"

    async def get_dependency_status(self, hass=None) -> Dict[str, Any]:
        """Get the status of all dependencies. @zara"""
        dependency_status = {}
        for package_name, required_version in REQUIRED_DEPENDENCIES.items():
            package_installed = self._package_status.get(package_name)
            if package_installed is None:
                if hass:
                    package_installed = await hass.async_add_executor_job(
                        self._check_package_sync, package_name
                    )
                else:
                    package_installed = self._check_package_sync(package_name)
                self._package_status[package_name] = package_installed
            if hass:
                package_version = await hass.async_add_executor_job(
                    self._get_package_version_sync, package_name
                )
            else:
                package_version = self._get_package_version_sync(package_name)
            dependency_status[package_name] = {
                "installed": package_installed,
                "version": package_version,
                "required": required_version,
                "satisfied": package_installed,
            }
        return dependency_status

    def get_installed_packages(self) -> list[str]:
        """Get list of installed package names. @zara"""
        if not self._checked:
            _LOGGER.warning("Dependencies not checked yet, returning empty list")
            return []
        return [
            package_name
            for package_name, installed in self._package_status.items()
            if installed
        ]

    def get_missing_packages(self) -> list[str]:
        """Get list of missing package names. @zara"""
        if not self._checked:
            _LOGGER.warning("Dependencies not checked yet, returning empty list")
            return []
        return [
            package_name
            for package_name, installed in self._package_status.items()
            if not installed
        ]
