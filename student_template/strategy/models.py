"""策略版本共用的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class TaskKind(str, Enum):
    """集中式调度中使用的任务类型。"""

    PICK_RAW = "pick_raw"
    DROP_MATERIAL = "drop_material"
    PICK_PRODUCT = "pick_product"
    DROP_PRODUCT = "drop_product"
    ABANDON = "abandon"


@dataclass(frozen=True)
class Task:
    """单辆车的任务描述。

    `pick_zone` 用于取货任务，`drop_zone` 用于投递任务。
    同时保留两个字段，便于后续版本扩展成两段式任务，而不破坏现有接口。
    """

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
class WorldView:
    """对原始状态快照做过规范化的只读视图。"""

    raw_state: dict[str, Any]
    time: float
    vehicles: dict[str, dict[str, Any]]
    zones: dict[str, dict[str, Any]]
    orders: list[dict[str, Any]]
    raw_items: set[str]
    product_items: set[str]
    processing_zones: list[str]
    raw_zones: list[str]
    consumer_zones: list[str]

    def pending_orders(self) -> list[dict[str, Any]]:
        return [
            order for order in self.orders
            if order.get("status", "pending") == "pending"
        ]


@dataclass
class StrategyConfig:
    """各版本共享的可调策略参数。"""

    cruise_speed: float = 20.0
    slow_speed: float = 6.0
    safety_distance: float = 6.0
    resume_distance: float = 8.0
    stale_task_seconds: float = 45.0
    enable_safety_speed: bool = False
    enable_forward_fill: bool = False
    enable_congestion_penalty: bool = False


@dataclass
class ActiveTask:
    """跨多个 tick 保留的活动任务，用于目标占用判断。"""

    vehicle_id: str
    task: Task
    assigned_at: float
    start_carrying: Optional[str]


@dataclass
class StrategyMemory:
    """策略对象持有的持久化记忆。"""

    active_tasks: dict[str, ActiveTask] = field(default_factory=dict)
    low_speed_vehicles: set[str] = field(default_factory=set)

    def prune(self, world: WorldView, stale_seconds: float) -> None:
        """清理已经完成或已经过期的活动任务。"""
        current_ids = set(world.vehicles)
        for vid in list(self.active_tasks):
            if vid not in current_ids:
                self.active_tasks.pop(vid, None)
                continue

            vehicle = world.vehicles[vid]
            active = self.active_tasks[vid]
            if vehicle.get("status") == "idle":
                self.active_tasks.pop(vid, None)
                continue

            if world.time - active.assigned_at > stale_seconds:
                self.active_tasks.pop(vid, None)
