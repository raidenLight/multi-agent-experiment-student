"""策略实现：单一类继承树，每个版本只重写自己改动的部分。

V1 → V2 → V3 → V4 → VN
"""

from __future__ import annotations

import random
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
        return 0

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
    # 任务校验（VN 重写加入超时释放）
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
        """只在有订单需求时才预补料，避免无需求时车辆空跑。"""
        # 统计当前订单需要哪些产品
        ordered_products = {order_product(o) for o in ctx["pending_orders"]}
        if not ordered_products:
            return None  # 没订单时不补料

        candidates = []
        for pzid in ctx["proc_zones"]:
            pz = ctx["zones"][pzid]
            # 只有产出品有订单需求的加工区才补料
            if not (set(pz.get("outputs", [])) & ordered_products):
                continue
            for item in pz.get("inputs", []):
                current = pz.get("items", {}).get(item, 0)
                in_transit = registry.material_in_transit(pzid, item)
                if current + in_transit >= 1:  # 每种原料只补 1 个
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
# V4：碰撞预警 + 低收益车辆重规划
# ======================================================================

class V4Strategy(V3Strategy):
    """V4：检测近距离车辆，低收益车避让重规划。"""

    COLLISION_WARN_DISTANCE = 3.5
    REPLAN_COOLDOWN_SECONDS = 4.0
    EXPECTED_GAIN_WEIGHT = 0.03
    RANDOM_REPLAN_PROB = 0.15
    BLOCK_LOOKAHEAD = 1
    DEFAULT_RAW_REWARD = 30.0
    DEFAULT_PRODUCT_REWARD = 100.0

    def __call__(self, state: dict) -> dict:
        ctx = self._prepare(state)
        self.memory.prune(ctx["vehicles"], ctx["time"], self.STALE_TASK_SECONDS)
        commands = self._compute_commands(ctx)
        commands.update(self._build_replan_overrides(ctx))
        self._update_last_nodes(ctx)
        return commands

    def _build_replan_overrides(self, ctx: dict) -> dict:
        if not self.sdk or len(ctx["vehicles"]) < 2:
            return {}

        cache = self._collect_vehicle_cache(ctx)
        if len(cache) < 2:
            return {}

        expected_gains = {
            vid: self._expected_gain(vid, ctx, cache)
            for vid in cache
        }

        replanned: set[str] = set()
        overrides: dict[str, dict] = {}
        vids = sorted(cache.keys(), key=vehicle_sort_key)

        for i in range(len(vids)):
            for j in range(i + 1, len(vids)):
                a, b = vids[i], vids[j]
                if a in replanned or b in replanned:
                    continue
                if self._distance(cache[a]["pos"], cache[b]["pos"]) > self.COLLISION_WARN_DISTANCE:
                    continue

                replanner = self._choose_replanner(a, b, ctx, expected_gains)
                if not replanner:
                    continue
                other = b if replanner == a else a

                cmd = self._replan_vehicle(replanner, other, ctx, cache)
                if cmd:
                    overrides[replanner] = cmd
                    replanned.add(replanner)
                    self.memory.last_replan_time[replanner] = ctx["time"]

        return overrides

    def _collect_vehicle_cache(self, ctx: dict) -> dict[str, dict]:
        cache: dict[str, dict] = {}
        for vid, v in ctx["vehicles"].items():
            pos = v.get("position")
            if not pos:
                continue
            current_node = self.sdk.find_nearest_node(pos[0], pos[1])
            next_node = self._preview_next_node(v, current_node)
            cache[vid] = {
                "pos": pos,
                "current_node": current_node,
                "next_node": next_node,
            }
        return cache

    def _preview_next_node(self, vehicle: dict, current_node: Optional[str]) -> Optional[str]:
        preview = vehicle.get("path_preview", [])
        if preview:
            node = self.sdk.find_nearest_node(preview[0][0], preview[0][1])
            return node or current_node
        return current_node

    def _expected_gain(self, vid: str, ctx: dict, cache: dict) -> float:
        active = self.memory.active_tasks.get(vid)
        if not active:
            return 0.0

        task = active.task
        reward = self._task_reward(task)
        dist = self._estimate_task_distance(cache[vid]["pos"], task)
        return reward - self.EXPECTED_GAIN_WEIGHT * dist

    def _task_reward(self, task: Task) -> float:
        if task.kind in {TaskKind.DROP_PRODUCT, TaskKind.PICK_PRODUCT}:
            return self._product_value(task.item or "")
        if task.kind in {TaskKind.DROP_MATERIAL, TaskKind.PICK_RAW}:
            if self.sdk and self.sdk.recipes:
                return float(self.sdk.recipes.get(task.item, {}).get("value", self.DEFAULT_RAW_REWARD))
            return self.DEFAULT_RAW_REWARD
        return 0.0

    def _estimate_task_distance(self, pos: list, task: Task) -> float:
        if not task.target_zone or not pos:
            return 0.0
        return self.sdk.zone_distance(pos, task.target_zone)

    @staticmethod
    def _distance(a: list, b: list) -> float:
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

    def _choose_replanner(self, a: str, b: str, ctx: dict,
                          expected_gains: dict[str, float]) -> Optional[str]:
        now = ctx["time"]
        a_cd = now - self.memory.last_replan_time.get(a, -1e9) < self.REPLAN_COOLDOWN_SECONDS
        b_cd = now - self.memory.last_replan_time.get(b, -1e9) < self.REPLAN_COOLDOWN_SECONDS
        if a_cd and b_cd:
            return None

        if random.random() < self.RANDOM_REPLAN_PROB:
            choice = random.choice([a, b])
        else:
            if expected_gains[a] == expected_gains[b]:
                choice = min(a, b)
            else:
                choice = a if expected_gains[a] < expected_gains[b] else b

        if choice == a and a_cd:
            return None if b_cd else b
        if choice == b and b_cd:
            return None if a_cd else a
        return choice

    def _replan_vehicle(self, vid: str, other: str, ctx: dict,
                        cache: dict[str, dict]) -> Optional[dict]:
        vehicle = ctx["vehicles"].get(vid, {})
        if vehicle.get("status") != "moving":
            return None

        active = self.memory.active_tasks.get(vid)
        if not active:
            return None

        task = active.task
        target_zone = task.target_zone
        action_type = task.action_type
        if not target_zone or not action_type:
            return None

        start = cache[vid]["current_node"]
        end = self.sdk.get_zone_node(target_zone)
        if not start or not end:
            return None

        blocked = self._build_blocked_nodes(vid, other, ctx, cache)
        blocked.discard(start)
        blocked.discard(end)

        path = self.sdk.plan_path_with_blocked(start, end, blocked)
        if path and len(path) > 1:
            return {
                "path": self.sdk.nodes_to_points(path),
                "action": {"type": action_type, "target_zone": target_zone},
                "speed": self.CRUISE_SPEED,
            }

        return self.sdk.navigate_to(
            target_zone,
            action={"type": action_type, "target_zone": target_zone},
            from_position=vehicle.get("position"),
            speed=self.CRUISE_SPEED,
        )

    def _build_blocked_nodes(self, vid: str, other: str, ctx: dict,
                             cache: dict[str, dict]) -> set[str]:
        blocked: set[str] = set()
        direction = self._direction_relation(vid, other, cache)
        if direction == "oncoming":
            prev_node = self.memory.last_nodes.get(other)
            if prev_node:
                blocked.add(prev_node)
            return blocked

        next_node = cache.get(other, {}).get("next_node")
        if next_node:
            blocked.add(next_node)

        preview = ctx["vehicles"].get(other, {}).get("path_preview", [])
        for point in preview[: self.BLOCK_LOOKAHEAD]:
            node = self.sdk.find_nearest_node(point[0], point[1])
            if node:
                blocked.add(node)

        return blocked

    def _direction_relation(self, a: str, b: str, cache: dict[str, dict]) -> str:
        last_a = self.memory.last_nodes.get(a)
        last_b = self.memory.last_nodes.get(b)
        next_a = cache.get(a, {}).get("next_node")
        next_b = cache.get(b, {}).get("next_node")

        if last_a and last_b and next_a and next_b:
            if next_a == last_b and next_b == last_a:
                return "oncoming"
            if next_a == next_b and next_a != cache[a].get("current_node"):
                return "same_direction"
        return "unknown"

    def _update_last_nodes(self, ctx: dict) -> None:
        for vid, v in ctx["vehicles"].items():
            pos = v.get("position")
            if not pos:
                continue
            node = self.sdk.find_nearest_node(pos[0], pos[1])
            if node:
                self.memory.last_nodes[vid] = node


# ======================================================================
# V5：时空联合路径规划
# ======================================================================

class V5Strategy(V4Strategy):
    """V5：继承 V4 拥堵路径 + 估算到达时间线，检测时空冲突并分级错峰。"""

    def __call__(self, state: dict) -> dict:
        ctx = self._prepare(state)
        self.memory.prune(ctx["vehicles"], ctx["time"], self.STALE_TASK_SECONDS)
        commands = self._compute_commands(ctx)
        return self._resolve_time_conflicts(commands)

    def _resolve_time_conflicts(self, commands: dict) -> dict:
        """估算每条路径的时间线，检测时间重叠的节点冲突。"""
        if len(commands) < 2:
            return commands

        timelines = {}
        for vid, cmd in commands.items():
            tl = self._build_timeline(cmd)
            if tl:
                timelines[vid] = tl

        vids = sorted(timelines.keys(), key=vehicle_sort_key)
        for i in range(len(vids)):
            for j in range(i + 1, len(vids)):
                a, b = vids[i], vids[j]
                conflict = self._find_time_conflict(timelines[a], timelines[b])
                if not conflict:
                    continue
                _node, _t_a, _t_b = conflict
                gap = abs(_t_a - _t_b)
                if gap < 1.0:
                    commands[b]["speed"] = 3.0
                elif gap < 2.0:
                    commands[b]["speed"] = 10.0
                else:
                    commands[b]["speed"] = 14.0
        return commands

    def _build_timeline(self, cmd: dict) -> list[tuple[str, float]]:
        """估算路径上各节点的到达时刻（距离 ÷ 速度）。"""
        pts = cmd.get("path", [])
        speed = cmd.get("speed", 20.0)
        if len(pts) < 2 or speed <= 0:
            return []
        timeline = []
        dist = 0.0
        prev = pts[0]
        for pt in pts[1:]:
            dist += self.sdk.distance(prev, pt)
            node = self.sdk.find_nearest_node(pt[0], pt[1])
            if node and (not timeline or timeline[-1][0] != node):
                timeline.append((node, dist / speed))
            prev = pt
        return timeline

    @staticmethod
    def _find_time_conflict(
            tl_a: list[tuple[str, float]],
            tl_b: list[tuple[str, float]]) -> tuple[str, float, float] | None:
        """找第一个 3s 内时间重叠的节点冲突。"""
        nodes_a = {n: t for n, t in tl_a}
        for node, t_b in tl_b:
            if node in nodes_a:
                t_a = nodes_a[node]
                if abs(t_a - t_b) < 3.0:
                    return (node, t_a, t_b)
        return None


# ======================================================================
# VN：超时任务释放
# ======================================================================

class VNStrategy(V5Strategy):
    """VN：超时任务释放——移动中车辆任务过久未完成则清空路径重新调度。"""

    def __call__(self, state: dict) -> dict:
        ctx = self._prepare(state)
        self.memory.prune(ctx["vehicles"], ctx["time"], self.STALE_TASK_SECONDS)
        commands = self._compute_commands(ctx)
        commands = self._resolve_time_conflicts(commands)

        # 超时任务强制释放
        for vid, active in list(self.memory.active_tasks.items()):
            vehicle = ctx["vehicles"].get(vid, {})
            if vehicle.get("status") != "moving":
                continue
            if ctx["time"] - active.assigned_at > self.STALE_TASK_SECONDS:
                if vid not in commands:
                    commands[vid] = {"path": [], "action": None}
                self.memory.active_tasks.pop(vid, None)

        return commands
