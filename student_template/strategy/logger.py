"""JSONL run logger for strategy debugging.

The logger is deliberately side-effect-light: it only writes diagnostic files
under ``logs/`` and never changes the command payload sent to the server.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RunLogger:
    """Write compact per-run JSONL diagnostics for a strategy version."""

    SNAPSHOT_INTERVAL_SECONDS = 1.0
    COMMAND_LOG_HEARTBEAT_SECONDS = 5.0

    def __init__(
            self,
            version: str,
            sdk: Any = None,
            log_root: Path | str | None = None,
            enabled: bool | None = None) -> None:
        self.version = version.lower()
        self.sdk = sdk
        self.log_root = Path(log_root) if log_root else PROJECT_ROOT / "logs"
        self.enabled = (
            os.environ.get("STRATEGY_LOG", "1").lower() not in {"0", "false", "no"}
            if enabled is None else enabled
        )
        self.path: Path | None = None
        self._fh = None
        self._started = False
        self._ended = False
        self._last_snapshot_time: float | None = None
        self._last_command_signature: dict[str, tuple] = {}
        self._last_command_log_time: dict[str, float] = {}
        self._last_event_log_time: dict[tuple, float] = {}
        self._config = self._load_config()

    def log_snapshot(self, state: dict, memory: Any = None) -> None:
        if not self.enabled or not isinstance(state, dict):
            return
        if state.get("type") == "game_over":
            self.log_end(state)
            return

        self._ensure_started(state)
        current_time = float(state.get("time", 0.0))
        if (self._last_snapshot_time is not None
                and current_time - self._last_snapshot_time < self.SNAPSHOT_INTERVAL_SECONDS):
            return
        self._last_snapshot_time = current_time

        vehicles = state.get("vehicles", {})
        self._write({
            "event": "snapshot",
            "sim_time": self._round(current_time),
            "score": self._round(state.get("score", 0.0)),
            "completed_orders_count": state.get("completed_orders_count", 0),
            "completed_orders_value": self._round(state.get("completed_orders_value", 0.0)),
            "drop_reward_total": self._round(state.get("drop_reward_total", 0.0)),
            "collision_penalty": self._round(state.get("collision_penalty", 0.0)),
            "overtime_penalty": self._round(state.get("overtime_penalty", 0.0)),
            "pending_orders": len([
                o for o in state.get("orders", [])
                if o.get("status") == "pending"
            ]),
            "vehicles": self._vehicle_summaries(vehicles, memory, current_time),
            "close_pairs": self._close_pairs(vehicles),
        })

    def log_command(
            self,
            state_or_ctx: dict,
            vehicle_id: str,
            command: dict,
            task: Any = None,
            source: str = "strategy") -> None:
        if not self.enabled or not command:
            return
        self._ensure_started(state_or_ctx)
        current_time = float(state_or_ctx.get("time", 0.0))
        action = command.get("action")
        if isinstance(action, dict):
            action_type = action.get("type")
            target_zone = action.get("target_zone")
        else:
            action_type = action
            target_zone = None

        path = command.get("path", []) or []
        signature = self._command_signature(source, action_type, target_zone, task)
        last_signature = self._last_command_signature.get(vehicle_id)
        last_time = self._last_command_log_time.get(vehicle_id, -1e9)
        is_heartbeat = (
            signature == last_signature
            and current_time - last_time >= self.COMMAND_LOG_HEARTBEAT_SECONDS
        )
        if signature == last_signature and not is_heartbeat:
            return

        self._last_command_signature[vehicle_id] = signature
        self._last_command_log_time[vehicle_id] = current_time
        self._write({
            "event": "command",
            "source": source,
            "sim_time": self._round(current_time),
            "heartbeat": is_heartbeat,
            "vehicle_id": vehicle_id,
            "action_type": action_type,
            "target_zone": target_zone,
            "path_points": len(path),
            "path_distance": self._round(self._points_distance(path)),
            "speed": self._round(command.get("speed")),
            "task": self._task_summary(task),
        })

    def log_event(self, event: str, state_or_ctx: dict | None = None, **payload: Any) -> None:
        if not self.enabled:
            return
        self._ensure_started(state_or_ctx or {})
        record = {
            "event": event,
            "sim_time": self._round((state_or_ctx or {}).get("time", 0.0)),
        }
        record.update(self._jsonable(payload))
        self._write(record)

    def log_event_throttled(
            self,
            event: str,
            key: Any,
            min_interval: float,
            state_or_ctx: dict | None = None,
            **payload: Any) -> None:
        if not self.enabled:
            return
        current_time = float((state_or_ctx or {}).get("time", 0.0))
        throttle_key = (event, self._hashable_key(key))
        last_time = self._last_event_log_time.get(throttle_key, -1e9)
        if current_time - last_time < min_interval:
            return
        self._last_event_log_time[throttle_key] = current_time
        self.log_event(event, state_or_ctx, **payload)

    def log_end(self, state: dict) -> None:
        if not self.enabled or self._ended:
            return
        self._ensure_started(state)
        self._ended = True
        self._write({
            "event": "run_end",
            "sim_time": self._round(state.get("time", 0.0)),
            "score": self._round(state.get("score", 0.0)),
            "completed_orders_count": state.get("completed_orders_count", 0),
            "completed_orders_value": self._round(state.get("completed_orders_value", 0.0)),
            "drop_reward_total": self._round(state.get("drop_reward_total", 0.0)),
            "collision_penalty": self._round(state.get("collision_penalty", 0.0)),
            "overtime_penalty": self._round(state.get("overtime_penalty", 0.0)),
            "random_seed": state.get("random_seed"),
        })
        if self._fh:
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    def _ensure_started(self, state_or_ctx: dict) -> None:
        if self._started:
            return

        seed = state_or_ctx.get("random_seed")
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{self.version}_seed{seed if seed is not None else 'unknown'}_{timestamp}_pid{os.getpid()}.jsonl"
        out_dir = self.log_root / self.version
        out_dir.mkdir(parents=True, exist_ok=True)
        self.path = out_dir / filename
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
        self._started = True

        graph = self._get_graph()
        game_cfg = self._config.get("game", {})
        self._write({
            "event": "run_start",
            "version": self.version,
            "created_at": timestamp,
            "pid": os.getpid(),
            "random_seed": seed,
            "server_url": getattr(self.sdk, "server_url", None),
            "duration": game_cfg.get("duration"),
            "tick_rate": game_cfg.get("tick_rate"),
            "collision_radius": getattr(self.sdk, "collision_radius", None),
            "map_width": graph.get("map_width"),
            "map_height": graph.get("map_height"),
            "graph_nodes": len(graph.get("nodes", {})),
            "graph_edges": len(graph.get("edges", [])),
        })

    def _write(self, record: dict) -> None:
        if not self._fh:
            return
        self._fh.write(json.dumps(self._jsonable(record), ensure_ascii=False, separators=(",", ":")) + "\n")

    def _vehicle_summaries(self, vehicles: dict, memory: Any, current_time: float) -> list[dict]:
        summaries = []
        active_tasks = getattr(memory, "active_tasks", {}) if memory else {}
        for vid in sorted(vehicles):
            vehicle = vehicles[vid]
            pos = vehicle.get("position") or []
            preview = vehicle.get("path_preview", []) or []
            active = active_tasks.get(vid)
            task = getattr(active, "task", None)
            assigned_at = getattr(active, "assigned_at", None)
            summaries.append({
                "id": vid,
                "position": self._point(pos),
                "status": vehicle.get("status"),
                "carrying": vehicle.get("carrying"),
                "speed": self._round(vehicle.get("speed")),
                "path_remaining": len(preview),
                "next_point": self._point(preview[0]) if preview else None,
                "nearest_node": self._nearest_node(pos),
                "active_task": self._task_summary(task),
                "task_age": self._round(current_time - assigned_at) if assigned_at is not None else None,
            })
        return summaries

    def _close_pairs(self, vehicles: dict) -> list[dict]:
        threshold = max(5.0, 2.0 * float(getattr(self.sdk, "collision_radius", 1.0)) + 2.0)
        pairs = []
        ids = sorted(vehicles)
        for i, a in enumerate(ids):
            pos_a = vehicles[a].get("position")
            if not pos_a:
                continue
            for b in ids[i + 1:]:
                pos_b = vehicles[b].get("position")
                if not pos_b:
                    continue
                dist = self._distance(pos_a, pos_b)
                if dist <= threshold:
                    pairs.append({"a": a, "b": b, "distance": self._round(dist)})
        return pairs

    def _task_summary(self, task: Any) -> dict | None:
        if not task:
            return None
        kind = getattr(task, "kind", None)
        return {
            "kind": getattr(kind, "value", kind),
            "item": getattr(task, "item", None),
            "pick_zone": getattr(task, "pick_zone", None),
            "drop_zone": getattr(task, "drop_zone", None),
            "target_zone": getattr(task, "target_zone", None),
            "order_id": getattr(task, "order_id", None),
            "priority": self._round(getattr(task, "priority", None)),
            "reason": getattr(task, "reason", None),
        }

    def _command_signature(
            self,
            source: str,
            action_type: Any,
            target_zone: Any,
            task: Any) -> tuple:
        kind = getattr(getattr(task, "kind", None), "value", getattr(task, "kind", None))
        return (
            source,
            action_type,
            target_zone,
            kind,
            getattr(task, "item", None),
            getattr(task, "pick_zone", None),
            getattr(task, "drop_zone", None),
            getattr(task, "order_id", None),
        )

    def _hashable_key(self, key: Any) -> Any:
        if isinstance(key, dict):
            return tuple(sorted((k, self._hashable_key(v)) for k, v in key.items()))
        if isinstance(key, (list, tuple, set)):
            return tuple(self._hashable_key(v) for v in key)
        if hasattr(key, "value"):
            return key.value
        return key

    def _points_distance(self, points: list) -> float:
        if hasattr(self.sdk, "points_distance"):
            try:
                return self.sdk.points_distance(points)
            except Exception:
                pass
        if len(points) < 2:
            return 0.0
        return sum(self._distance(points[i - 1], points[i]) for i in range(1, len(points)))

    def _nearest_node(self, pos: list) -> str | None:
        if not pos or not hasattr(self.sdk, "find_nearest_node"):
            return None
        try:
            return self.sdk.find_nearest_node(pos[0], pos[1])
        except Exception:
            return None

    def _get_graph(self) -> dict:
        if hasattr(self.sdk, "get_graph"):
            try:
                return self.sdk.get_graph() or {}
            except Exception:
                return {}
        return {}

    def _load_config(self) -> dict:
        try:
            with open(PROJECT_ROOT / "config.json", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def _distance(a: list, b: list) -> float:
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

    @staticmethod
    def _point(point: list) -> list[float] | None:
        if not point:
            return None
        return [round(float(point[0]), 3), round(float(point[1]), 3)]

    @staticmethod
    def _round(value: Any) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
            if not math.isfinite(number):
                return None
            return round(number, 3)
        except (TypeError, ValueError):
            return value

    def _jsonable(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): self._jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._jsonable(v) for v in value]
        if hasattr(value, "value"):
            return value.value
        return value
