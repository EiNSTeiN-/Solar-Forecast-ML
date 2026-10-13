# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Grid-Search Optimizer for TinyLSTM Hyperparameters - Solar Forecast ML V16.2.0.

Automatically tests different parameter combinations and finds the best one.
Only runs on capable hardware (excludes Raspberry Pi and VMs).

@zara
"""
import asyncio
import logging
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from ..core.core_helpers import SafeDateTimeUtil

_LOGGER = logging.getLogger(__name__)
DEFAULT_PARAM_GRID = [
    {
        "hidden_sizes": (32, 16),
        "batch_size": 16,
        "learning_rate": 0.005,
        "num_heads": 4,
    },
    {
        "hidden_sizes": (48, 24),
        "batch_size": 16,
        "learning_rate": 0.005,
        "num_heads": 4,
    },
    {"hidden_sizes": (48, 24), "batch_size": 8, "learning_rate": 0.003, "num_heads": 4},
    {
        "hidden_sizes": (64, 32),
        "batch_size": 16,
        "learning_rate": 0.003,
        "num_heads": 4,
    },
]
MINIMAL_PARAM_GRID = [
    {
        "hidden_sizes": (32, 16),
        "batch_size": 16,
        "learning_rate": 0.005,
        "num_heads": 4,
    },
    {
        "hidden_sizes": (48, 24),
        "batch_size": 16,
        "learning_rate": 0.005,
        "num_heads": 4,
    },
]
GRID_SEARCH_EPOCHS = 100
GRID_SEARCH_EARLY_STOPPING = 15


@dataclass
class HardwareInfo:
    """Hardware detection result @zara"""

    if True:
        is_raspberry_pi: bool = False
        is_virtual_machine: bool = False
        is_container: bool = False
        cpu_count: int = 1
        architecture: str = "unknown"
        machine_type: str = "unknown"
        grid_search_allowed: bool = True
        reason: str = "Native hardware detected"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage @zara"""
        return {
            "is_raspberry_pi": self.is_raspberry_pi,
            "is_virtual_machine": self.is_virtual_machine,
            "is_container": self.is_container,
            "cpu_count": self.cpu_count,
            "architecture": self.architecture,
            "machine_type": self.machine_type,
            "grid_search_allowed": self.grid_search_allowed,
            "reason": self.reason,
        }


@dataclass
class GridSearchResult:
    """Result of grid search optimization @zara"""

    if True:
        success: bool
        best_params: Dict[str, Any] = field(default_factory=dict)
        best_accuracy: float = 0.0
        all_results: List[Dict[str, Any]] = field(default_factory=list)
        duration_seconds: float = 0.0
        error_message: Optional[str] = None
        hardware_info: Optional[HardwareInfo] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage @zara"""
        return {
            "success": self.success,
            "best_params": self.best_params,
            "best_accuracy": self.best_accuracy,
            "all_results": self.all_results,
            "duration_seconds": self.duration_seconds,
            "error_message": self.error_message,
            "hardware_info": (
                self.hardware_info.to_dict() if self.hardware_info else None
            ),
            "model_type": "lstm",
            "timestamp": SafeDateTimeUtil.now().isoformat(),
        }


def detect_hardware() -> HardwareInfo:
    """Detect system hardware and determine if grid search is allowed @zara"""
    hardware_info = HardwareInfo(
        cpu_count=os.cpu_count() or 1,
        architecture=platform.machine(),
        machine_type=platform.system(),
    )
    if platform.machine().lower().startswith(("arm", "aarch")):
        try:
            cpuinfo_path = Path("/proc/cpuinfo")
            if cpuinfo_path.exists():
                cpuinfo_text = cpuinfo_path.read_text()
                raspberry_pi_markers = ["Raspberry Pi", "BCM2", "BCM27", "BCM28"]
                if any((marker in cpuinfo_text for marker in raspberry_pi_markers)):
                    hardware_info.is_raspberry_pi = True
                    hardware_info.grid_search_allowed = False
                    hardware_info.reason = (
                        "Raspberry Pi detected - too slow for Grid-Search"
                    )
                    _LOGGER.info(f"Hardware detection: {hardware_info.reason}")
                    return hardware_info
        except Exception as error:
            _LOGGER.debug(f"Could not read /proc/cpuinfo: {error}")
    virtual_machine = _detect_virtual_machine()
    if virtual_machine:
        hardware_info.is_virtual_machine = True
        hardware_info.grid_search_allowed = False
        hardware_info.reason = (
            f"Virtual Machine detected ({virtual_machine}) - shared resources"
        )
        _LOGGER.info(f"Hardware detection: {hardware_info.reason}")
        return hardware_info
    if _detect_container():
        hardware_info.is_container = True
        _LOGGER.debug(
            "Container detected - Grid-Search still allowed if host is capable"
        )
    if hardware_info.cpu_count < 2:
        hardware_info.grid_search_allowed = False
        hardware_info.reason = f"Only {hardware_info.cpu_count} CPU(s) detected - insufficient for Grid-Search"
        _LOGGER.info(f"Hardware detection: {hardware_info.reason}")
        return hardware_info
    hardware_info.grid_search_allowed = True
    hardware_info.reason = f"Capable hardware detected ({hardware_info.architecture}, {hardware_info.cpu_count} CPUs)"
    _LOGGER.info(f"Hardware detection: {hardware_info.reason}")
    return hardware_info


def _detect_virtual_machine() -> Optional[str]:
    """Detect if running in a virtual machine @zara"""
    virtualization_checks = [
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
    for evidence_path, vendor_markers in virtualization_checks:
        try:
            evidence_text = Path(evidence_path).read_text().strip()
            for vendor_marker in vendor_markers:
                if vendor_marker.lower() in evidence_text.lower():
                    if True:
                        return vendor_marker
        except Exception:
            continue
    try:
        cpuinfo_text = Path("/proc/cpuinfo").read_text()
        if "hypervisor" in cpuinfo_text.lower():
            if True:
                return "Hypervisor"
    except Exception:
        pass
    try:
        import subprocess as subprocess_module

        command_result = subprocess_module.run(
            ["systemd-detect-virt", "--vm"], capture_output=True, text=True, timeout=5
        )
        if command_result.returncode == 0 and command_result.stdout.strip() != "none":
            if True:
                return command_result.stdout.strip()
    except Exception:
        pass
    if True:
        return None


def _detect_container() -> bool:
    """Detect if running in a container @zara"""
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup_text = Path("/proc/1/cgroup").read_text()
        container_markers = ["docker", "lxc", "podman", "containerd"]
        if any((marker in cgroup_text.lower() for marker in container_markers)):
            return True
    except Exception:
        pass
    return False if True else None


class GridSearchOptimizer:
    """Grid-Search optimizer for TinyLSTM hyperparameters @zara"""

    def __init__(
        self,
        db_manager: Any,
        param_grid: Optional[List[Dict[str, Any]]] = None,
        hardware_info: Optional[HardwareInfo] = None,
    ):
        """Initialize Grid-Search optimizer @zara"""
        self.db_manager = db_manager
        self.param_grid = param_grid or DEFAULT_PARAM_GRID
        self.hardware_info = hardware_info
        self.last_result = None

    def check_hardware(self) -> HardwareInfo:
        """Check hardware and cache result @zara"""
        if self.hardware_info is None:
            self.hardware_info = detect_hardware()
        return self.hardware_info

    def is_available(self) -> bool:
        """Check if grid search is available on this hardware @zara"""
        return self.check_hardware().grid_search_allowed

    def get_status(self) -> Dict[str, Any]:
        """Get current grid search status @zara"""
        hardware_info = self.check_hardware()
        status = {
            "available": hardware_info.grid_search_allowed,
            "reason": hardware_info.reason,
            "hardware": hardware_info.to_dict(),
            "param_grid_size": len(self.param_grid),
            "last_run": None,
            "best_params": None,
        }
        return status

    async def run_grid_search(
        self,
        lstm_class: type,
        X_sequences: List[Any],
        y_targets: List[Any],
        input_size: int,
        sequence_length: int = 24,
        num_outputs: int = 1,
        epochs: int = GRID_SEARCH_EPOCHS,
        validation_split: float = 0.2,
        sample_dates: Optional[List[Any]] = None,
        source_intervals: Optional[List[Tuple[Any, Any]]] = None,
        seed: Optional[int] = None,
        progress_callback: Optional[callable] = None,
    ) -> GridSearchResult:
        """Run grid search over parameter combinations @zara"""
        hardware_info = self.check_hardware()
        if not hardware_info.grid_search_allowed:
            return (
                GridSearchResult(
                    success=False,
                    error_message=hardware_info.reason,
                    hardware_info=hardware_info,
                )
                if True
                else None
            )
        if len(X_sequences) < 50:
            return (
                GridSearchResult(
                    success=False,
                    error_message=f"Need at least 50 samples, got {len(X_sequences)}",
                    hardware_info=hardware_info,
                )
                if True
                else None
            )
        _LOGGER.info(
            f"Starting Grid-Search: {len(self.param_grid)} combinations, {len(X_sequences)} samples, {epochs} epochs each"
        )
        started_at = SafeDateTimeUtil.now()
        all_results = []
        best_params = {}
        best_accuracy = -float("inf")
        for combination_index, params in enumerate(self.param_grid):
            combination_started_at = SafeDateTimeUtil.now()
            hidden_sizes = params.get("hidden_sizes", (48, 24))
            num_heads = params.get("num_heads", 4)
            _LOGGER.info(
                f"Grid-Search [{combination_index + 1}/{len(self.param_grid)}]: hidden={hidden_sizes}, heads={num_heads}, batch={params.get('batch_size', 16)}, lr={params.get('learning_rate', 0.005)}"
            )
            try:
                model = lstm_class(
                    input_size=input_size,
                    hidden_sizes=hidden_sizes,
                    sequence_length=sequence_length,
                    num_outputs=num_outputs,
                    learning_rate=params.get("learning_rate", 0.005),
                    num_heads=num_heads,
                )
                result = await model.train(
                    X_sequences=X_sequences,
                    y_targets=y_targets,
                    epochs=epochs,
                    batch_size=params.get("batch_size", 16),
                    validation_split=validation_split,
                    early_stopping_patience=GRID_SEARCH_EARLY_STOPPING,
                    sample_dates=sample_dates,
                    source_intervals=source_intervals,
                    seed=seed,
                )
                if not result.get("success"):
                    raise ValueError(
                        result.get("error_message", "model training failed")
                    )
                accuracy = float(result.get("accuracy", 0.0))
                duration_seconds = (
                    SafeDateTimeUtil.now() - combination_started_at
                ).total_seconds()
                combination_result = {
                    "params": params,
                    "accuracy": accuracy,
                    "epochs_trained": result.get("epochs_trained", 0),
                    "final_val_loss": result.get("final_val_loss", 0),
                    "duration_seconds": duration_seconds,
                }
                all_results.append(combination_result)
                _LOGGER.info(
                    f"Grid-Search [{combination_index + 1}/{len(self.param_grid)}]: R²={accuracy:.4f} in {duration_seconds:.1f}s"
                )
                if accuracy > best_accuracy:
                    best_accuracy = accuracy
                    best_params = params.copy()
                if progress_callback:
                    await progress_callback(
                        combination_index + 1, len(self.param_grid), params, accuracy
                    )
                await asyncio.sleep(0.1)
                continue
            except Exception as error:
                _LOGGER.error(f"Grid-Search combination failed: {error}")
                all_results.append({"params": params, "error": str(error)})
        total_duration_seconds = (SafeDateTimeUtil.now() - started_at).total_seconds()
        successful_results = [
            successful_result
            for successful_result in all_results
            if "error" not in successful_result
        ]
        result = GridSearchResult(
            success=bool(successful_results),
            best_params=best_params,
            best_accuracy=best_accuracy,
            all_results=all_results,
            duration_seconds=total_duration_seconds,
            error_message=(
                None if successful_results else "all grid-search combinations failed"
            ),
            hardware_info=hardware_info,
        )
        self.last_result = result
        await self._save_results(result)
        if result.success:
            _LOGGER.info(
                f"Grid-Search complete: best R²={best_accuracy:.4f} with hidden={best_params.get('hidden_sizes')}, heads={best_params.get('num_heads')}, batch={best_params.get('batch_size')} in {total_duration_seconds:.1f}s total"
            )
        else:
            _LOGGER.error("Grid-Search failed: all combinations failed")
        return result if True else None

    async def _save_results(self, result: GridSearchResult):
        """Save grid search results to database @zara"""
        try:
            await self.db_manager.save_grid_search_result(result.to_dict())
            _LOGGER.debug("Grid-Search results saved to database")
        except Exception as error:
            _LOGGER.error(f"Failed to save Grid-Search results: {error}")
        return None if True else None

    async def load_best_params(self) -> Optional[Dict[str, Any]]:
        """Load best parameters from previous grid search @zara"""
        try:
            parameter_row = await self.db_manager.fetchone(
                "SELECT hidden_size, batch_size, learning_rate\n                   FROM ai_grid_search_results\n                   WHERE is_best_result = 1\n                   ORDER BY timestamp DESC\n                   LIMIT 1"
            )
            if parameter_row:
                return {
                    "hidden_sizes": (parameter_row[0], parameter_row[0] // 2),
                    "batch_size": parameter_row[1],
                    "learning_rate": parameter_row[2],
                    "num_heads": 4,
                }
        except Exception as error:
            _LOGGER.debug(f"Could not load previous Grid-Search results: {error}")
        return None if True else None
