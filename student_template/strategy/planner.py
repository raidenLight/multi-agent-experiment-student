"""各策略版本的任务规划逻辑。"""

from __future__ import annotations

from typing import Optional

from .models import Task, TaskKind, WorldView
from .registry import ClaimRegistry
from .utils import (
    order_consumer,
    order_deadline,
    order_id,
    order_product,
    urgency_score,
)


class V1TaskPlanner:
    """V1 规划器：V0 贪心规则加目标占用检查。"""

    def __init__(self, sdk=None) -> None:
        self.sdk = sdk

    def choose_task(
            self,
            vehicle_id: str,
            vehicle: dict,
            world: WorldView,
            registry: ClaimRegistry) -> Optional[Task]:
        carrying = vehicle.get("carrying")
        if carrying and carrying in world.raw_items:
            return self._choose_material_drop(carrying, world, registry)
        if carrying:
            return self._choose_product_drop(carrying, world, registry)
        return self._choose_empty_vehicle_task(world, registry)

    def _choose_material_drop(
            self,
            item: str,
            world: WorldView,
            registry: ClaimRegistry) -> Optional[Task]:
        fallback = None
        for zid in world.processing_zones:
            zone = world.zones[zid]
            if item not in zone.get("inputs", []):
                continue

            task = Task(
                kind=TaskKind.DROP_MATERIAL,
                item=item,
                drop_zone=zid,
                priority=20.0,
                reason=f"将 {item} 送到加工区",
            )
            if fallback is None and registry.can_claim(task):
                fallback = task

            current = zone.get("items", {}).get(item, 0)
            in_transit = registry.material_in_transit(zid, item)
            if current + in_transit < 1 and registry.can_claim(task):
                return task
        return fallback

    def _choose_product_drop(
            self,
            item: str,
            world: WorldView,
            registry: ClaimRegistry) -> Optional[Task]:
        orders = sorted(world.pending_orders(), key=order_deadline)
        for order in orders:
            if order_product(order) != item:
                continue
            consumer = order_consumer(order)
            if not consumer:
                continue
            zone = world.zones.get(consumer, {})
            if not zone.get("ready", True):
                continue
            task = Task(
                kind=TaskKind.DROP_PRODUCT,
                item=item,
                drop_zone=consumer,
                order_id=order_id(order),
                priority=self._order_priority(world, order),
                reason=f"将 {item} 送到订单 {order_id(order)}",
            )
            if registry.can_claim(task):
                return task
        return None

    def _choose_empty_vehicle_task(
            self,
            world: WorldView,
            registry: ClaimRegistry) -> Optional[Task]:
        product_task = self._choose_ready_product_pick(world, registry)
        if product_task:
            return product_task
        return self._choose_raw_pick_for_orders(world, registry)

    def _choose_ready_product_pick(
            self,
            world: WorldView,
            registry: ClaimRegistry) -> Optional[Task]:
        orders = sorted(world.pending_orders(), key=order_deadline)
        for order in orders:
            product = order_product(order)
            for zid in world.processing_zones:
                zone = world.zones[zid]
                if product not in zone.get("outputs", []):
                    continue
                if not zone.get("ready"):
                    continue
                task = Task(
                    kind=TaskKind.PICK_PRODUCT,
                    item=product,
                    pick_zone=zid,
                    order_id=order_id(order),
                    priority=self._order_priority(world, order),
                    reason=f"拾取已完成成品 {product}",
                )
                if registry.can_claim(task):
                    return task
        return None

    def _choose_raw_pick_for_orders(
            self,
            world: WorldView,
            registry: ClaimRegistry) -> Optional[Task]:
        orders = sorted(world.pending_orders(), key=order_deadline)
        for order in orders:
            product = order_product(order)
            for pzid in world.processing_zones:
                pz = world.zones[pzid]
                if product not in pz.get("outputs", []):
                    continue
                for needed_item in pz.get("inputs", []):
                    current = pz.get("items", {}).get(needed_item, 0)
                    in_transit = registry.material_in_transit(pzid, needed_item)
                    if current + in_transit >= 1:
                        continue
                    task = self._first_available_raw_pick(needed_item, world, registry)
                    if task:
                        return task
        return None

    def _first_available_raw_pick(
            self,
            item: str,
            world: WorldView,
            registry: ClaimRegistry) -> Optional[Task]:
        for rzid in world.raw_zones:
            rz = world.zones[rzid]
            if item not in rz.get("outputs", []):
                continue
            if not rz.get("ready"):
                continue
            task = Task(
                kind=TaskKind.PICK_RAW,
                item=item,
                pick_zone=rzid,
                priority=10.0,
                reason=f"拾取原料 {item}",
            )
            if registry.can_claim(task):
                return task
        return None

    def _order_priority(self, world: WorldView, order: dict) -> float:
        deadline = float(order.get("deadline", float("inf")))
        return 1000.0 * urgency_score(world.time, deadline)


class V2TaskPlanner(V1TaskPlanner):
    """V2 占位：基于收益、距离和紧急度的任务分配。"""

    def _order_priority(self, world: WorldView, order: dict) -> float:
        """V2 核心扩展点：订单价值、距离和紧急度综合评分。"""
        return super()._order_priority(world, order)

    def _estimate_task_distance(self, vehicle: dict, task: Task) -> float:
        """V2 核心扩展点：估计车辆执行任务的路程成本。"""
        if not self.sdk or not task.target_zone:
            return 0.0
        return self.sdk.zone_distance(vehicle.get("position"), task.target_zone)


class V3TaskPlanner(V2TaskPlanner):
    """V3 占位：前馈补料和产能预测。"""

    def _choose_forward_fill_task(
            self,
            world: WorldView,
            registry: ClaimRegistry) -> Optional[Task]:
        """V3 核心扩展点：没有紧急任务时，提前给加工区补料。"""
        return None

    def _estimate_processing_need(self, world: WorldView) -> dict[str, list[str]]:
        """V3 核心扩展点：预测各加工区后续缺少的原料。"""
        return {}
