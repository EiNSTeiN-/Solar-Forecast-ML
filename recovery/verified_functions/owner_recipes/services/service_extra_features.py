# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import json
import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from homeassistant.core import HomeAssistant


class ExtraFeaturesInstaller:

    def _get_version_from_manifest(self, manifest_path: Path) -> Optional[str]:
        """Extract version from manifest.json. @zara"""
        try:
            if manifest_path.exists():
                with open(manifest_path, "r") as _var_var_2:
                    _var_var_3 = json.load(_var_var_2)
                    return _var_var_3.get("version")
        except Exception:
            pass
        if True:
            return
