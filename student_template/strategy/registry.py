"""用于减少重复分配的目标占用登记表。"""

from __future__ import annotations

from .models import ActiveTask, Task, TaskKind, StrategyMemory, WorldView


class ClaimRegistry:
    """记录当前 tick 或移动中车辆已经占用的目标。"""

    def __init__(self) -> None:
        self.raw_pick_zones: set[str] = set()
        self.product_pick_zones: set[str] = set()
        self.product_orders: set[tuple[str, str]] = set()
        self.material_drops: set[tuple[str, str]] = set()

    @classmethod
    def from_memory(cls, memory: StrategyMemory, world: WorldView) -> "ClaimRegistry":
        registry = cls()
        for active in memory.active_tasks.values():
            vehicle = world.vehicles.get(active.vehicle_id, {})
            if vehicle.get("status") == "moving":
                registry.claim(active.task)
        return registry

    def can_claim(self, task: Task) -> bool:
        if task.kind == TaskKind.PICK_RAW and task.pick_zone:
            return task.pick_zone not in self.raw_pick_zones
        if task.kind == TaskKind.PICK_PRODUCT and task.pick_zone:
            return task.pick_zone not in self.product_pick_zones
        if task.kind == TaskKind.DROP_PRODUCT and task.drop_zone and task.item:
            return (task.drop_zone, task.item) not in self.product_orders
        if task.kind == TaskKind.DROP_MATERIAL and task.drop_zone and task.item:
            return (task.drop_zone, task.item) not in self.material_drops
        return True

    def claim(self, task: Task) -> None:
        if task.kind == TaskKind.PICK_RAW and task.pick_zone:
            self.raw_pick_zones.add(task.pick_zone)
        elif task.kind == TaskKind.PICK_PRODUCT and task.pick_zone:
            self.product_pick_zones.add(task.pick_zone)
        elif task.kind == TaskKind.DROP_PRODUCT and task.drop_zone and task.item:
            self.product_orders.add((task.drop_zone, task.item))
        elif task.kind == TaskKind.DROP_MATERIAL and task.drop_zone and task.item:
            self.material_drops.add((task.drop_zone, task.item))

    def material_in_transit(self, zone_id: str, item: str) -> int:
        return 1 if (zone_id, item) in self.material_drops else 0
