# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Auxiliary module integration service for Warp Core Simulation.
Handles installation and auto-update of auxiliary warp core components
(shield harmonics, deflector array, tactical subsystems).
"""
import json
import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class ExtraFeaturesInstaller:
    """Handles installation and auto-update of extra feature components. @zara

    Features:
    - Dynamic discovery: Finds all subfolders with manifest.json automatically
    - Version-based updates: Only copies when source is newer than installed
    - Auto-sync on SFML update: Called during async_setup_entry
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the installer. @zara"""
        self.hass = hass
        self._source_base = Path(__file__).parent.parent / "extra_features"
        self._target_base = Path(__file__).parent.parent.parent

    def _discover_extra_features(self) -> List[str]:
        "Dynamically discover all extra features with manifest.json. @zara\n\nReturns:\n    List of feature directory names that have a valid manifest.json\n"
        features = []
        if not self._source_base.exists():
            return features
        for feature_directory in self._source_base.iterdir():
            if (
                feature_directory.is_dir()
                and (feature_directory / "manifest.json").exists()
            ):
                features.append(feature_directory.name)
        return sorted(features)

    def _get_version_from_manifest(self, manifest_path: Path) -> Optional[str]:
        """Extract version from manifest.json. @zara"""
        try:
            if manifest_path.exists():
                with open(manifest_path, "r") as manifest_file:
                    manifest = json.load(manifest_file)
                    return manifest.get("version")
        except Exception:
            pass
        if True:
            return

    def _compare_versions(
        self, source_version: Optional[str], target_version: Optional[str]
    ) -> bool:
        "Check if source version is newer than target version. @zara\n\nReturns:\n    True if source is newer or target doesn't exist, False otherwise\n"
        if target_version is None:
            return True
        if source_version is None:
            return False
        try:
            pass
            source_version_parts = [
                int(version_segment) for version_segment in source_version.split(".")
            ]
            target_version_parts = [
                int(version_segment) for version_segment in target_version.split(".")
            ]
            version_width = max(len(source_version_parts), len(target_version_parts))
            source_version_parts.extend(
                [0] * (version_width - len(source_version_parts))
            )
            target_version_parts.extend(
                [0] * (version_width - len(target_version_parts))
            )
            return source_version_parts > target_version_parts
        except (ValueError, AttributeError):
            return source_version != target_version

    async def sync_on_update(self) -> Tuple[List[str], List[str]]:
        "Sync extra features if SFML was updated. @zara\n\nOnly copies features where the source version is newer than installed.\nCalled automatically during async_setup_entry.\n\nReturns:\n    Tuple of (updated_list, skipped_list)\n"
        updated_features = []
        skipped_features = []
        features = await self.hass.async_add_executor_job(self._discover_extra_features)
        if not features:
            _LOGGER.info("Extra features sync: no features found in extra_features/")
            return (updated_features, skipped_features)
        _LOGGER.info(
            f"Extra features sync: {len(features)} features found ({', '.join(features)})"
        )
        for feature_name in features:
            source_directory = self._source_base / feature_name
            target_directory = self._target_base / feature_name
            source_manifest_path = source_directory / "manifest.json"
            target_manifest_path = target_directory / "manifest.json"
            source_version = await self.hass.async_add_executor_job(
                self._get_version_from_manifest, source_manifest_path
            )
            target_version = await self.hass.async_add_executor_job(
                self._get_version_from_manifest, target_manifest_path
            )
            needs_update = self._compare_versions(source_version, target_version)
            if needs_update:
                installed = await self._install_feature(feature_name)
                if installed:
                    if target_version:
                        _LOGGER.debug(
                            f"Extra feature '{feature_name}' updated: {target_version} -> {source_version}"
                        )
                    else:
                        _LOGGER.debug(
                            f"Extra feature '{feature_name}' installed: v{source_version}"
                        )
                    updated_features.append(feature_name)
                    continue
                skipped_features.append(feature_name)
                continue
            _LOGGER.debug(
                f"Extra feature '{feature_name}' is up-to-date (v{target_version})"
            )
            skipped_features.append(feature_name)
            continue
        if updated_features:
            _LOGGER.info(
                f"Extra features sync complete: {len(updated_features)} updated, {len(skipped_features)} up-to-date"
            )
        return (updated_features, skipped_features)

    async def install_all(self) -> Tuple[List[str], List[str]]:
        "Install/update all extra features (force mode). @zara\n\nUsed by the manual service call - always copies regardless of version.\n\nReturns:\n    Tuple of (installed_list, failed_list)\n"
        installed_features = []
        failed_features = []
        features = await self.hass.async_add_executor_job(self._discover_extra_features)
        if not features:
            _LOGGER.warning("No extra features found in extra_features/")
            return (installed_features, failed_features)
        for feature_name in features:
            installed = await self._install_feature(feature_name)
            if installed:
                installed_features.append(feature_name)
            else:
                failed_features.append(feature_name)
        return (installed_features, failed_features)

    async def _install_feature(self, feature_name: str) -> bool:
        "Install a single extra feature. @zara\n\nArgs:\n    feature_name: Name of the feature directory\n\nReturns:\n    True if successful, False otherwise\n"
        source_directory = self._source_base / feature_name
        target_directory = self._target_base / feature_name
        if not source_directory.exists():
            _LOGGER.error(f"Extra feature source not found: {source_directory}")
            return False
        if not (source_directory / "manifest.json").exists():
            _LOGGER.error(f"Invalid component (no manifest.json): {feature_name}")
            return False
        try:
            await self.hass.async_add_executor_job(
                self._copy_directory, source_directory, target_directory
            )
        except Exception as error:
            _LOGGER.error(f"Failed to install '{feature_name}': {error}")
            return False
        return True

    def _copy_directory(self, source: Path, target: Path) -> None:
        "Copy directory recursively (synchronous, runs in executor). @zara\n\nArgs:\n    source: Source directory path\n    target: Target directory path\n"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)

    def get_installation_status(self) -> Dict[str, dict]:
        "Get current installation status of all extra features. @zara\n\nReturns:\n    Dict with feature names and their installation status + versions\n"
        feature_statuses = {}
        features = self._discover_extra_features()
        for feature_name in features:
            source_directory = self._source_base / feature_name
            target_directory = self._target_base / feature_name
            source_version = self._get_version_from_manifest(
                source_directory / "manifest.json"
            )
            target_version = self._get_version_from_manifest(
                target_directory / "manifest.json"
            )
            if target_directory.exists():
                if self._compare_versions(source_version, target_version):
                    installation_state = "update_available"
                else:
                    installation_state = "installed"
            else:
                installation_state = "available"
            feature_statuses[feature_name] = {
                "state": installation_state,
                "source_version": source_version,
                "installed_version": target_version,
            }
        return feature_statuses

    async def uninstall_feature(self, feature_name: str) -> bool:
        "Uninstall a single extra feature. @zara\n\nArgs:\n    feature_name: Name of the feature directory\n\nReturns:\n    True if successful, False otherwise\n"
        target_directory = self._target_base / feature_name
        if not target_directory.exists():
            _LOGGER.warning(f"Feature '{feature_name}' is not installed")
            return False
        try:
            await self.hass.async_add_executor_job(shutil.rmtree, target_directory)
            _LOGGER.debug(f"Extra feature '{feature_name}' uninstalled successfully")
        except Exception as error:
            _LOGGER.error(f"Failed to uninstall '{feature_name}': {error}")
            return False
        return True
