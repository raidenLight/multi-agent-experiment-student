"""策略版本共用的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TaskKind(str, Enum):
    PICK_RAW = "pick_raw"
    DROP_MATERIAL = "drop_material"
    PICK_PRODUCT = "pick_product"
    DROP_PRODUCT = "drop_product"
    ABANDON = "abandon"
    WAIT = "wait"


@dataclass
class Task:
    """单辆车的一次任务。"""

    kind: TaskKind
    item: Optional[str] = None
    pick_zone: Optional[str] = None
    drop_zone: Optional[str] = None
    order_id: Optional[str] = None
    priority: float = 0.0
    reason: str = ""

    @property
    def target_zone(self) -> Optional[str]:
        if self.kind in {TaskKind.PICK_RAW, TaskKind.PICK_PRODUCT}:
            return self.pick_zone
        if self.kind in {TaskKind.DROP_MATERIAL, TaskKind.DROP_PRODUCT}:
            return self.drop_zone
        return self.drop_zone or self.pick_zone

    @property
    def action_type(self) -> Optional[str]:
        if self.kind in {TaskKind.PICK_RAW, TaskKind.PICK_PRODUCT}:
            return "pick"
        if self.kind in {TaskKind.DROP_MATERIAL, TaskKind.DROP_PRODUCT}:
            return "drop"
        if self.kind == TaskKind.ABANDON:
            return "abandon"
        return None


@dataclass
class ActiveTask:
    """跨 tick 保留的活动任务，用于目标占用判断。"""

    vehicle_id: str
    task: Task
    assigned_at: float
    start_carrying: Optional[str]


@dataclass
class StrategyMemory:
    """策略持久记忆：活动任务 + 降速车辆。"""

    active_tasks: dict[str, ActiveTask] = field(default_factory=dict)
    low_speed_vehicles: set[str] = field(default_factory=set)
    last_nodes: dict[str, str] = field(default_factory=dict)
    last_replan_time: dict[str, float] = field(default_factory=dict)
    last_clearance_time: dict[str, float] = field(default_factory=dict)
    reserved_clearance_nodes: set[str] = field(default_factory=set)
    reserved_staging_nodes: set[str] = field(default_factory=set)
    close_pair_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    close_pair_first_seen: dict[tuple[str, str], float] = field(default_factory=dict)
    close_pair_last_seen: dict[tuple[str, str], float] = field(default_factory=dict)
    hot_nodes: dict[str, float] = field(default_factory=dict)
    deferred_tasks: dict[str, Task] = field(default_factory=dict)

    def prune(self, vehicles: dict, current_time: float, stale_seconds: float) -> None:
        """清理已完成或过期的活动任务。"""
        for vid in list(self.active_tasks):
            if vid not in vehicles:
                self.active_tasks.pop(vid, None)
                continue
            vehicle = vehicles[vid]
            active = self.active_tasks[vid]
            if vehicle.get("status") == "idle":
                self.active_tasks.pop(vid, None)
            elif current_time - active.assigned_at > stale_seconds:
                self.active_tasks.pop(vid, None)

        for vid in list(self.last_nodes):
            if vid not in vehicles:
                self.last_nodes.pop(vid, None)

        for vid in list(self.last_replan_time):
            if vid not in vehicles:
                self.last_replan_time.pop(vid, None)

        for vid in list(self.last_clearance_time):
            if vid not in vehicles:
                self.last_clearance_time.pop(vid, None)

        for vid in list(self.deferred_tasks):
            if vid not in vehicles:
                self.deferred_tasks.pop(vid, None)

        active_vehicle_ids = set(vehicles)
        for pair in list(self.close_pair_counts):
            if pair[0] not in active_vehicle_ids or pair[1] not in active_vehicle_ids:
                self.close_pair_counts.pop(pair, None)
                self.close_pair_first_seen.pop(pair, None)
                self.close_pair_last_seen.pop(pair, None)

        for pair, seen_at in list(self.close_pair_last_seen.items()):
            if current_time - seen_at > 5.0:
                self.close_pair_counts.pop(pair, None)
                self.close_pair_first_seen.pop(pair, None)
                self.close_pair_last_seen.pop(pair, None)

        for node, seen_at in list(self.hot_nodes.items()):
            if current_time - seen_at > 5.0:
                self.hot_nodes.pop(node, None)
