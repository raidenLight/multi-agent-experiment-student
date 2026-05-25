"""策略实现：单一类继承树，每个版本只重写自己改动的部分。

V1 → V2 → V3 → V4 → VN
"""

from __future__ import annotations

from typing import Optional

from .models import ActiveTask, StrategyMemory, Task, TaskKind
from .registry import ClaimRegistry
from .utils import (
    order_consumer,
    order_deadline,
    order_id,
    order_product,
    urgency_score,
    vehicle_sort_key,
)


class V1Strategy:
    """V1：V0 贪心 + target_zone + claimed 目标占用 + 距离排序。"""

    CRUISE_SPEED = 20.0
    STALE_TASK_SECONDS = 45.0

    def __init__(self, sdk) -> None:
        self.sdk = sdk
        self.memory = StrategyMemory()

    # ==================================================================
    # 每 tick 入口
    # ==================================================================

    def __call__(self, state: dict) -> dict:
        ctx = self._prepare(state)
        self.memory.prune(ctx["vehicles"], ctx["time"], self.STALE_TASK_SECONDS)
        return self._compute_commands(ctx)

    def _prepare(self, state: dict) -> dict:
        """从原始 state 预计算常用字段，返回普通字典。"""
        zones = state.get("zones", {})
        raw_zones, proc_zones, cons_zones = [], [], []
        raw_items, prod_items = set(), set()

        for zid, z in zones.items():
            t = z.get("type")
            if t == "raw_material":
                raw_zones.append(zid)
                raw_items.update(z.get("outputs", []))
            elif t == "processing":
                proc_zones.append(zid)
                prod_items.update(z.get("outputs", []))
            elif t == "consumer":
                cons_zones.append(zid)

        orders = state.get("orders", [])
        return {
            "time": float(state.get("time", 0.0)),
            "vehicles": state.get("vehicles", {}),
            "zones": zones,
            "orders": orders,
            "pending_orders": [o for o in orders if o.get("status") == "pending"],
            "raw_zones": raw_zones,
            "proc_zones": proc_zones,
            "cons_zones": cons_zones,
            "raw_items": raw_items,
            "prod_items": prod_items,
        }

    def _compute_commands(self, ctx: dict) -> dict:
        """遍历空闲车辆，分配任务、构造指令。"""
        registry = ClaimRegistry.from_memory(self.memory, ctx["vehicles"])
        commands = {}

        for vid in sorted(ctx["vehicles"], key=vehicle_sort_key):
            vehicle = ctx["vehicles"][vid]
            if vehicle.get("status") != "idle":
                continue

            task = self._choose_task(vid, vehicle, ctx, registry)
            task = self._validate_task(task, vehicle, ctx)
            if not task:
                continue

            command = self._build_command(vehicle, task, ctx)
            if not command:
                continue

            registry.claim(task)
            self.memory.active_tasks[vid] = ActiveTask(
                vehicle_id=vid, task=task,
                assigned_at=ctx["time"],
                start_carrying=vehicle.get("carrying"),
            )
            commands[vid] = command

        return commands

    # ==================================================================
    # 任务选择（V2/V3 重写相关方法）
    # ==================================================================

    def _choose_task(self, vehicle_id: str, vehicle: dict,
                     ctx: dict, registry: ClaimRegistry) -> Optional[Task]:
        carrying = vehicle.get("carrying")
        pos = vehicle.get("position")

        if carrying and carrying in ctx["raw_items"]:
            return self._choose_material_drop(pos, carrying, ctx, registry)
        if carrying:
            return self._choose_product_drop(carrying, ctx, registry)
        return self._choose_empty_vehicle_task(pos, ctx, registry)

    def _choose_material_drop(self, pos: list, item: str, ctx: dict,
                              registry: ClaimRegistry) -> Optional[Task]:
        """送原料到最近且缺货的加工区。"""
        candidates = []
        for zid in ctx["proc_zones"]:
            zone = ctx["zones"][zid]
            if item not in zone.get("inputs", []):
                continue
            current = zone.get("items", {}).get(item, 0)
            in_transit = registry.material_in_transit(zid, item)
            if current + in_transit >= 1:
                continue

            task = Task(kind=TaskKind.DROP_MATERIAL, item=item, drop_zone=zid,
                        priority=20.0, reason=f"送 {item} 到 {zid}")
            if registry.can_claim(task):
                candidates.append((self._zone_distance(pos, zid), task))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
        return None

    def _choose_product_drop(self, item: str, ctx: dict,
                             registry: ClaimRegistry) -> Optional[Task]:
        """送成品到有对应订单的消费区。"""
        for order in self._sorted_orders(ctx):
            if order_product(order) != item:
                continue
            consumer = order_consumer(order)
            if not consumer or not ctx["zones"].get(consumer, {}).get("ready"):
                continue

            task = Task(kind=TaskKind.DROP_PRODUCT, item=item, drop_zone=consumer,
                        order_id=order_id(order),
                        priority=self._order_priority(ctx, order),
                        reason=f"送 {item} 到订单 {order_id(order)}")
            if registry.can_claim(task):
                return task
        return None

    def _choose_empty_vehicle_task(self, pos: list, ctx: dict,
                                   registry: ClaimRegistry) -> Optional[Task]:
        """空车：优先取成品 → 反推原料取货。"""
        return (self._choose_ready_product_pick(pos, ctx, registry)
                or self._choose_raw_pick_for_orders(pos, ctx, registry))

    def _choose_ready_product_pick(self, pos: list, ctx: dict,
                                   registry: ClaimRegistry) -> Optional[Task]:
        """取最近的已完成成品。"""
        candidates = []
        for order in self._sorted_orders(ctx):
            product = order_product(order)
            for zid in ctx["proc_zones"]:
                zone = ctx["zones"][zid]
                if product not in zone.get("outputs", []) or not zone.get("ready"):
                    continue
                task = Task(kind=TaskKind.PICK_PRODUCT, item=product, pick_zone=zid,
                            order_id=order_id(order),
                            priority=self._order_priority(ctx, order),
                            reason=f"取成品 {product}")
                if registry.can_claim(task):
                    candidates.append((self._zone_distance(pos, zid), task))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
        return None

    def _choose_raw_pick_for_orders(self, pos: list, ctx: dict,
                                    registry: ClaimRegistry) -> Optional[Task]:
        """根据订单缺口，反推需要取的原料。"""
        for order in self._sorted_orders(ctx):
            product = order_product(order)
            for pzid in ctx["proc_zones"]:
                pz = ctx["zones"][pzid]
                if product not in pz.get("outputs", []):
                    continue
                for needed_item in pz.get("inputs", []):
                    current = pz.get("items", {}).get(needed_item, 0)
                    in_transit = registry.material_in_transit(pzid, needed_item)
                    if current + in_transit >= 1:
                        continue
                    task = self._pick_nearest_raw(pos, needed_item, ctx, registry)
                    if task:
                        return task
        return None

    def _pick_nearest_raw(self, pos: list, item: str, ctx: dict,
                          registry: ClaimRegistry) -> Optional[Task]:
        """取最近的可用原料。"""
        candidates = []
        for rzid in ctx["raw_zones"]:
            rz = ctx["zones"][rzid]
            if item not in rz.get("outputs", []) or not rz.get("ready"):
                continue
            task = Task(kind=TaskKind.PICK_RAW, item=item, pick_zone=rzid,
                        priority=10.0, reason=f"取原料 {item}")
            if registry.can_claim(task):
                candidates.append((self._zone_distance(pos, rzid), task))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
        return None

    # ==================================================================
    # 订单排序与优先级（V2 重写）
    # ==================================================================

    def _sorted_orders(self, ctx: dict) -> list[dict]:
        return sorted(ctx["pending_orders"], key=order_deadline)

    def _order_priority(self, ctx: dict, order: dict) -> float:
        deadline = float(order.get("deadline", float("inf")))
        return 1000.0 * urgency_score(ctx["time"], deadline)

    # ==================================================================
    # 命令构造（V4 重写加入拥堵感知）
    # ==================================================================

    def _build_command(self, vehicle: dict, task: Task,
                       ctx: dict = None) -> Optional[dict]:
        if task.kind == TaskKind.WAIT:
            return {"path": [], "action": None, "speed": 0.0}
        if task.kind == TaskKind.ABANDON:
            return {"path": [], "action": {"type": "abandon"}}

        target_zone = task.target_zone
        action_type = task.action_type
        if not target_zone or not action_type:
            return None

        return self.sdk.navigate_to(
            target_zone,
            action={"type": action_type, "target_zone": target_zone},
            from_position=vehicle.get("position"),
            speed=self.CRUISE_SPEED,
        )

    # ==================================================================
    # 任务校验（VN 重写）
    # ==================================================================

    def _validate_task(self, task: Optional[Task], vehicle: dict,
                       ctx: dict) -> Optional[Task]:
        return task

    # ==================================================================
    # 辅助
    # ==================================================================

    def _zone_distance(self, pos, zone_id: str) -> float:
        if not self.sdk or not pos:
            return float("inf")
        return self.sdk.zone_distance(pos, zone_id)


# ======================================================================
# V2：收益 + 紧急度综合排序
# ======================================================================

class V2Strategy(V1Strategy):
    """订单按 收益×1.0 + 紧急度×80 排序。"""

    def _sorted_orders(self, ctx: dict) -> list[dict]:
        return sorted(
            ctx["pending_orders"],
            key=lambda o: self._order_priority(ctx, o),
            reverse=True,
        )

    def _order_priority(self, ctx: dict, order: dict) -> float:
        deadline = float(order.get("deadline", float("inf")))
        urgency = urgency_score(ctx["time"], deadline)
        value = self._product_value(order_product(order))
        return value * 1.0 + urgency * 80.0

    def _product_value(self, product: str) -> float:
        if self.sdk and self.sdk.recipes:
            return float(self.sdk.recipes.get(product, {}).get("value", 100))
        return 100.0


# ======================================================================
# V3：前馈补料
# ======================================================================

class V3Strategy(V2Strategy):
    """空闲时预存加工区原料，减少成品等待。"""

    def _choose_empty_vehicle_task(self, pos: list, ctx: dict,
                                   registry: ClaimRegistry) -> Optional[Task]:
        task = super()._choose_empty_vehicle_task(pos, ctx, registry)
        return task or self._choose_forward_fill_task(pos, ctx, registry)

    def _choose_forward_fill_task(self, pos: list, ctx: dict,
                                  registry: ClaimRegistry) -> Optional[Task]:
        """没有紧急任务时，提前给加工区补料（每种原料最多预存 2 个）。"""
        candidates = []
        for pzid in ctx["proc_zones"]:
            pz = ctx["zones"][pzid]
            for item in pz.get("inputs", []):
                current = pz.get("items", {}).get(item, 0)
                in_transit = registry.material_in_transit(pzid, item)
                if current + in_transit >= 2:
                    continue
                for rzid in ctx["raw_zones"]:
                    rz = ctx["zones"][rzid]
                    if item not in rz.get("outputs", []) or not rz.get("ready"):
                        continue
                    task = Task(kind=TaskKind.PICK_RAW, item=item, pick_zone=rzid,
                                priority=5.0, reason=f"前馈补料: 取 {item}")
                    if registry.can_claim(task):
                        candidates.append((self._zone_distance(pos, rzid), task))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
        return None


# ======================================================================
# V4：拥堵感知路径 + 局部避碰降速
# ======================================================================

class V4Strategy(V3Strategy):
    """路径绕开拥堵节点 + 近距离降速避碰。"""

    SLOW_SPEED = 6.0
    SAFETY_DISTANCE = 6.0

    def __call__(self, state: dict) -> dict:
        ctx = self._prepare(state)
        self.memory.prune(ctx["vehicles"], ctx["time"], self.STALE_TASK_SECONDS)
        commands = self._compute_commands(ctx)
        return self._apply_safety_speed(commands, ctx)

    def _build_command(self, vehicle: dict, task: Task,
                       ctx: dict = None) -> Optional[dict]:
        if task.kind in (TaskKind.WAIT, TaskKind.ABANDON):
            return super()._build_command(vehicle, task, ctx)

        target_zone = task.target_zone
        action_type = task.action_type
        if not target_zone or not action_type:
            return None

        pos = vehicle.get("position")
        if ctx:
            penalties = self._build_node_penalties(ctx)
            start = self.sdk.find_nearest_node(pos[0], pos[1])
            end = self.sdk.get_zone_node(target_zone)
            if start and end and penalties:
                path = self.sdk.plan_path_with_penalty(start, end, penalties)
                if path:
                    return {
                        "path": self.sdk.nodes_to_points(path),
                        "action": {"type": action_type, "target_zone": target_zone},
                        "speed": self.CRUISE_SPEED,
                    }

        return self.sdk.navigate_to(
            target_zone,
            action={"type": action_type, "target_zone": target_zone},
            from_position=pos,
            speed=self.CRUISE_SPEED,
        )

    def _build_node_penalties(self, ctx: dict) -> dict[str, float]:
        """车辆聚集的节点加惩罚权重。"""
        penalties: dict[str, float] = {}
        for _vid, v in ctx["vehicles"].items():
            pos = v.get("position")
            if not pos:
                continue
            node = self.sdk.find_nearest_node(pos[0], pos[1])
            if node:
                penalties[node] = penalties.get(node, 0.0) + 50.0
            for point in v.get("path_preview", [])[:3]:
                pnode = self.sdk.find_nearest_node(point[0], point[1])
                if pnode:
                    penalties[pnode] = penalties.get(pnode, 0.0) + 30.0
        return penalties

    def _apply_safety_speed(self, commands: dict, ctx: dict) -> dict:
        for vid, cmd in commands.items():
            vehicle = ctx["vehicles"].get(vid)
            if not vehicle:
                continue
            if self._has_close_vehicle(vid, vehicle, ctx["vehicles"]):
                cmd["speed"] = self.SLOW_SPEED
                self.memory.low_speed_vehicles.add(vid)
            elif vid in self.memory.low_speed_vehicles:
                cmd["speed"] = self.CRUISE_SPEED
                self.memory.low_speed_vehicles.discard(vid)
        return commands

    def _has_close_vehicle(self, vid: str, vehicle: dict,
                           vehicles: dict) -> bool:
        pos = vehicle.get("position")
        for oid, o in vehicles.items():
            if oid == vid:
                continue
            if vehicle_sort_key(oid) > vehicle_sort_key(vid):
                continue
            if self.sdk.distance(pos, o.get("position")) < self.SAFETY_DISTANCE:
                return True
        return False


# ======================================================================
# VN：完整协同 + 动态校验
# ======================================================================

class VNStrategy(V4Strategy):
    """完整协同调度 + 任务有效性校验。"""

    def _validate_task(self, task: Optional[Task], vehicle: dict,
                       ctx: dict) -> Optional[Task]:
        if not task:
            return None
        if task.target_zone and task.target_zone not in ctx["zones"]:
            return None
        if task.kind == TaskKind.DROP_PRODUCT:
            order = ctx["zones"].get(task.drop_zone, {}).get("order")
            if not order or order.get("status") != "pending":
                return None
        if task.kind == TaskKind.DROP_MATERIAL:
            zone = ctx["zones"].get(task.drop_zone, {})
            if task.item not in zone.get("inputs", []):
                return None
        return task
