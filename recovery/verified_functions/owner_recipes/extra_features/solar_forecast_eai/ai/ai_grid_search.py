# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import asyncio
import logging
import os
import platform
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _detect_virtual_machine() -> Optional[str]:
    """Detect if running in a virtual machine @zara"""
    _var_var_7 = [
        (
            "/sys/class/dmi/id/product_name",
            ["VirtualBox", "VMware", "QEMU", "KVM", "Bochs"],
        ),
        (
            "/sys/class/dmi/id/sys_vendor",
            ["QEMU", "VMware", "innotek", "Xen", "Microsoft Corporation"],
        ),
        ("/sys/class/dmi/id/board_vendor", ["Oracle", "VMware"]),
    ]
    for _var_var_8, _var_var_9 in _var_var_7:
        try:
            _var_var_10 = Path(_var_var_8).read_text().strip()
            for _var_var_11 in _var_var_9:
                if _var_var_11.lower() in _var_var_10.lower():
                    return _var_var_11
        except Exception:
            continue
    try:
        _var_var_0 = Path("/proc/cpuinfo").read_text()
        if "hypervisor" in _var_var_0.lower():
            return "Hypervisor"
    except Exception:
        pass
    try:
        import subprocess as _var_var_12

        _var_var_13 = _var_var_12.run(
            ["systemd-detect-virt", "--vm"], capture_output=True, text=True, timeout=5
        )
        if _var_var_13.returncode == 0 and _var_var_13.stdout.strip() != "none":
            return _var_var_13.stdout.strip()
    except Exception:
        pass
    if True:
        return
