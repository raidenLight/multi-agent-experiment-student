"""Adapters for unique V5-VN standalone snapshots.

The snapshots keep a sizeable amount of tuned strategy code as module-level
state.  These adapters let the existing ``student_vX.py -> VXStrategy`` entry
points run that code without disturbing the class hierarchy used by VN.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


class V5VNSnapshotStrategy:
    """Run one V5-VN snapshot through the current SDK wrapper."""

    SNAPSHOT_FILENAME = ""

    def __init__(self, sdk: Any, source_path: str | Path | None = None, *, quiet: bool = True) -> None:
        self.sdk = sdk
        self.quiet = quiet
        self.source_path = Path(source_path) if source_path is not None else self._default_source_path()
        self._module = self._load_snapshot()
        self.reset()

    @classmethod
    def _default_source_path(cls) -> Path:
        if not cls.SNAPSHOT_FILENAME:
            raise ValueError(f"{cls.__name__} must set SNAPSHOT_FILENAME")
        workspace_root = Path(__file__).resolve().parents[3]
        return workspace_root / "V5-VN" / cls.SNAPSHOT_FILENAME

    @staticmethod
    def _framework_root() -> Path:
        return Path(__file__).resolve().parents[2]

    def _load_snapshot(self) -> ModuleType:
        if not self.source_path.exists():
            raise FileNotFoundError(f"V5-VN snapshot not found: {self.source_path}")

        framework_root = str(self._framework_root())
        if framework_root not in sys.path:
            sys.path.insert(0, framework_root)

        safe_name = self.SNAPSHOT_FILENAME.replace(".", "_").replace("-", "_")
        module_name = f"_codex_v5_vn_snapshot_{safe_name}_{id(self)}"
        spec = importlib.util.spec_from_file_location(module_name, self.source_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Unable to load V5-VN snapshot: {self.source_path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def reset(self) -> None:
        """Bind the framework SDK and clear the snapshot's per-run state."""

        self._module.sdk = self.sdk
        if hasattr(self._module, "ASSIGNMENTS"):
            self._module.ASSIGNMENTS = {}
        if hasattr(self._module, "PARKING_CACHE_KEY"):
            self._module.PARKING_CACHE_KEY = None
        if hasattr(self._module, "LAST_LOG_TIME"):
            self._module.LAST_LOG_TIME = float("inf") if self.quiet else -999.0

    def __call__(self, state: dict[str, Any] | None) -> dict[str, Any]:
        if not state or state.get("type") == "game_over":
            return {}

        self._module.sdk = self.sdk
        if self.quiet and hasattr(self._module, "LAST_LOG_TIME"):
            self._module.LAST_LOG_TIME = float("inf")

        commands = self._module.my_strategy(state)
        return commands or {}


class V5VN3170Strategy(V5VNSnapshotStrategy):
    """V5: seed100 3170 snapshot."""

    SNAPSHOT_FILENAME = "student_3170_seed100.py"


class V5VN3280Strategy(V5VNSnapshotStrategy):
    """V6: seed100 3280 snapshot."""

    SNAPSHOT_FILENAME = "student_3280_seed100.py"


class V5VN3457Strategy(V5VNSnapshotStrategy):
    """V7: seed100 3457 baked snapshot."""

    SNAPSHOT_FILENAME = "student_best_mean_4098_seed100_3457_baked.py"


class V5VN3590Strategy(V5VNSnapshotStrategy):
    """V8: seed100 3590 online v11 snapshot."""

    SNAPSHOT_FILENAME = "student_online_v11_exact_3590.py"


class V5VN3768Strategy(V5VNSnapshotStrategy):
    """V9: seed100 3768 snapshot."""

    SNAPSHOT_FILENAME = "student_3768_seed100.py"


LowestSnapshotV5Strategy = V5VN3170Strategy
