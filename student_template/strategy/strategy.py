"""策略实现：单一类继承树，每个版本只重写自己改动的部分。

任务分配线：V1 → V2 → V3 → V3_1
路径协同线：V2 → V4 → V5 → VN
"""

from __future__ import annotations

import os
import random
from typing import Optional

from .logger import RunLogger
from .models import ActiveTask, StrategyMemory, Task, TaskKind
from .registry import ClaimRegistry
from .utils import (
    hungarian_assign,
    order_consumer,
    order_deadline,
    order_id,
    order_product,
    urgency_score,
    vehicle_sort_key,
)


class V1Strategy:
    """V1：V0 贪心 + target_zone + claimed 目标占用。无评分和距离排序。"""

    CRUISE_SPEED = 20.0
    STALE_TASK_SECONDS = 45.0

    def __init__(self, sdk) -> None:
        self.sdk = sdk
        self.memory = StrategyMemory()
        self.logger = RunLogger(self._strategy_version(), sdk)

    # ==================================================================
    # 每 tick 入口
    # ==================================================================

    def __call__(self, state: dict) -> dict:
        if self._handle_terminal_state(state):
            return {}
        self._dist_cache.clear()
        ctx = self._prepare(state)
        self.memory.prune(ctx["vehicles"], ctx["time"], self.STALE_TASK_SECONDS)
        commands = self._compute_commands(ctx)
        self.logger.log_snapshot(state, self.memory)
        return commands

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
            "random_seed": state.get("random_seed"),
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
        self._augment_registry(registry, ctx)
        commands = {}

        for vid in sorted(ctx["vehicles"], key=vehicle_sort_key):
            vehicle = ctx["vehicles"][vid]
            if vehicle.get("status") != "idle":
                continue

            task = self._choose_task(vid, vehicle, ctx, registry)
            if not task:
                continue

            command = self._build_command(
                vehicle, task, ctx, vehicle_id=vid, registry=registry
            )
            if not command:
                continue

            registry.claim(task)
            self.memory.active_tasks[vid] = ActiveTask(
                vehicle_id=vid, task=task,
                assigned_at=ctx["time"],
                start_carrying=vehicle.get("carrying"),
            )
            commands[vid] = command
            self.logger.log_command(ctx, vid, command, task=task)

        return commands

    def _augment_registry(self, registry: ClaimRegistry, ctx: dict) -> None:
        """Hook for versions that add extra occupancy constraints."""
        return None

    def _target_zone_available(self, pos: list, zone_id: str, ctx: dict,
                               task: Task = None) -> bool:
        """Hook for versions that consider physical target occupancy."""
        return True

    # ==================================================================
    # 任务选择 — V1: V0 决策树 + claim 检查
    # ==================================================================

    def _choose_task(self, vehicle_id: str, vehicle: dict,
                     ctx: dict, registry: ClaimRegistry) -> Optional[Task]:
        carrying = vehicle.get("carrying")
        pos = vehicle.get("position")

        if carrying and carrying in ctx["raw_items"]:
            return self._choose_material_drop(pos, carrying, ctx, registry)
        if carrying:
            return self._choose_product_drop(pos, carrying, ctx, registry)
        return self._choose_empty_vehicle_task(pos, ctx, registry)

    def _choose_material_drop(self, pos: list, item: str, ctx: dict,
                              registry: ClaimRegistry) -> Optional[Task]:
        """送原料到缺货加工区（首个匹配）。"""
        for zid in ctx["proc_zones"]:
            zone = ctx["zones"][zid]
            if item not in zone.get("inputs", []):
                continue
            if zone.get("items", {}).get(item, 0) + registry.material_in_transit(zid, item) >= 1:
                continue

            task = Task(kind=TaskKind.DROP_MATERIAL, item=item, drop_zone=zid,
                        reason=f"送 {item} 到 {zid}")
            if not self._target_zone_available(pos, zid, ctx, task):
                continue
            if registry.can_claim(task):
                return task
        return None

    def _choose_product_drop(self, pos: list, item: str, ctx: dict,
                             registry: ClaimRegistry) -> Optional[Task]:
        """送成品到有对应订单的消费区（首个匹配）。"""
        orders = sorted(ctx["pending_orders"], key=order_deadline)
        for order in orders:
            if order_product(order) != item:
                continue
            consumer = order_consumer(order)
            if consumer and ctx["zones"].get(consumer, {}).get("ready"):
                task = Task(kind=TaskKind.DROP_PRODUCT, item=item, drop_zone=consumer,
                            order_id=order_id(order), reason=f"送 {item} 到 {consumer}")
                if not self._target_zone_available(pos, consumer, ctx, task):
                    continue
                if registry.can_claim(task):
                    return task
        return None

    def _choose_empty_vehicle_task(self, pos: list, ctx: dict,
                                   registry: ClaimRegistry) -> Optional[Task]:
        """空车：取成品 → 取原料（首个匹配）。"""
        orders = sorted(ctx["pending_orders"], key=order_deadline)
        # 1. 取已完成成品
        for order in orders:
            product = order_product(order)
            for zid in ctx["proc_zones"]:
                zone = ctx["zones"][zid]
                if product in zone.get("outputs", []) and zone.get("ready"):
                    task = Task(kind=TaskKind.PICK_PRODUCT, item=product, pick_zone=zid,
                                order_id=order_id(order), reason=f"取成品 {product}")
                    if not self._target_zone_available(pos, zid, ctx, task):
                        continue
                    if registry.can_claim(task):
                        return task
        # 2. 取订单缺口需要的原料
        for order in orders:
            product = order_product(order)
            for pzid in ctx["proc_zones"]:
                pz = ctx["zones"][pzid]
                if product not in pz.get("outputs", []):
                    continue
                for needed_item in pz.get("inputs", []):
                    if pz.get("items", {}).get(needed_item, 0) + registry.material_in_transit(pzid, needed_item) >= 1:
                        continue
                    for rzid in ctx["raw_zones"]:
                        rz = ctx["zones"][rzid]
                        if needed_item in rz.get("outputs", []) and rz.get("ready"):
                            task = Task(kind=TaskKind.PICK_RAW, item=needed_item,
                                        pick_zone=rzid, reason=f"取原料 {needed_item}")
                            if not self._target_zone_available(pos, rzid, ctx, task):
                                continue
                            if registry.can_claim(task):
                                return task
        return None

    # ==================================================================
    # 命令构造 / 校验
    # ==================================================================

    def _build_command(self, vehicle: dict, task: Task,
                       ctx: dict = None, vehicle_id: str = None,
                       registry: ClaimRegistry = None) -> Optional[dict]:
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

    # 距离缓存：每个 tick 内复用 Dijkstra 结果，减少重复寻路
    _dist_cache: dict = {}

    def _zone_distance(self, pos, zone_id: str) -> float:
        """缓存的 zone 距离（per-tick, 基于最近节点 key）。"""
        if not self.sdk or not pos or not zone_id:
            return float("inf")
        # 用最近节点作为 cache key（同一节点出发到同一 zone 距离相同）
        node = self.sdk.find_nearest_node(pos[0], pos[1])
        key = (node, zone_id)
        if key not in self._dist_cache:
            self._dist_cache[key] = self.sdk.zone_distance(pos, zone_id)
        return self._dist_cache[key]

    def _handle_terminal_state(self, state: dict) -> bool:
        if isinstance(state, dict) and state.get("type") == "game_over":
            self.logger.log_end(state)
            return True
        return False

    def _strategy_version(self) -> str:
        name = self.__class__.__name__.lower()
        if name.endswith("strategy"):
            name = name[:-8]
        return name


# ======================================================================
# V2：综合评分 + 距离排序
# ======================================================================

class V2Strategy(V1Strategy):
    """V2：订单价值驱动的综合评分调度。

    核心改进（相对V1）：
    1. 所有候选统一打分，按 综合评分 = 订单贡献 - 距离折扣 选最优
    2. 取原料任务的评分链接到下游订单价值（order_score * 0.5），而非固定值
    3. 取成品任务考虑后续 delivery 距离（前瞻），避免只选近的pick点忽视远的delivery
    4. 送原料选最近缺货加工区（纯距离排序，避免无意义长距离运送）
    """

    # ==================================================================
    # 评分常量（可通过环境变量微调）
    # ==================================================================
    PICK_DISTANCE_WEIGHT = float(os.environ.get("V2_PICK_DIST_WEIGHT", "4.0"))
    DELIVERY_LOOKAHEAD_WEIGHT = float(os.environ.get("V2_DELIV_LOOKAHEAD", "1.2"))
    DROP_DISTANCE_WEIGHT = float(os.environ.get("V2_DROP_DIST_WEIGHT", "4.0"))

    # ==================================================================
    # 评分组件
    # ==================================================================

    def _order_priority(self, ctx: dict, order: dict) -> float:
        """订单综合评分 = 收益 + 紧急度。"""
        deadline = float(order.get("deadline", float("inf")))
        urgency = urgency_score(ctx["time"], deadline)
        value = self._product_value(order_product(order))
        return value * 100.0 + urgency * 100.0

    def _product_value(self, product: str) -> float:
        if not self.sdk or not self.sdk.recipes:
            return 0.0
        return float(self.sdk.recipes.get(product, {}).get("value", 0))

    def _material_scarcity(self, item: str, ctx: dict) -> float:
        """原料紧缺度 = 缺该原料的加工区数 ÷ 可取的原料区数。"""
        need = sum(1 for zid in ctx["proc_zones"]
                   if item in ctx["zones"][zid].get("inputs", [])
                   and ctx["zones"][zid].get("items", {}).get(item, 0) == 0)
        have = sum(1 for zid in ctx["raw_zones"]
                   if item in ctx["zones"][zid].get("outputs", [])
                   and ctx["zones"][zid].get("ready"))
        return need / max(have, 1)

    def _lookahead_distance(self, from_zone_id: str, to_zone_id: str) -> float:
        """缓存的 zone→zone delivery 距离。"""
        if not self.sdk or not to_zone_id:
            return 0.0
        if not hasattr(self, '_lookahead_cache'):
            self._lookahead_cache: dict[tuple[str, str], float] = {}
        key = (from_zone_id, to_zone_id)
        if key in self._lookahead_cache:
            return self._lookahead_cache[key]
        zone_pos = self.sdk.get_zone_position(from_zone_id)
        if not zone_pos:
            return 0.0
        dist = self._zone_distance(zone_pos, to_zone_id)
        if len(self._lookahead_cache) < 200:
            self._lookahead_cache[key] = dist
        return dist

    def _sorted_orders(self, ctx: dict) -> list[dict]:
        return sorted(ctx["pending_orders"],
                      key=lambda o: self._order_priority(ctx, o), reverse=True)

    # ==================================================================
    # 任务选择 — 统一打分，选最优
    # ==================================================================

    def _choose_material_drop(self, pos: list, item: str, ctx: dict,
                              registry: ClaimRegistry) -> Optional[Task]:
        """送原料到最近且缺货的加工区（纯距离排序）。"""
        candidates = []
        for zid in ctx["proc_zones"]:
            zone = ctx["zones"][zid]
            if item not in zone.get("inputs", []):
                continue
            if zone.get("items", {}).get(item, 0) + registry.material_in_transit(zid, item) >= 1:
                continue
            task = Task(kind=TaskKind.DROP_MATERIAL, item=item, drop_zone=zid,
                        priority=20.0, reason=f"送 {item} 到 {zid}")
            if not self._target_zone_available(pos, zid, ctx, task):
                continue
            if registry.can_claim(task):
                dist = self._zone_distance(pos, zid)
                score = 20.0 - dist * self.DROP_DISTANCE_WEIGHT
                candidates.append((score, task))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
        return None

    def _choose_product_drop(self, pos: list, item: str, ctx: dict,
                             registry: ClaimRegistry) -> Optional[Task]:
        """送成品到评分最高的订单消费区（首个匹配）。"""
        for order in self._sorted_orders(ctx):
            if order_product(order) != item:
                continue
            consumer = order_consumer(order)
            if consumer and ctx["zones"].get(consumer, {}).get("ready"):
                task = Task(kind=TaskKind.DROP_PRODUCT, item=item, drop_zone=consumer,
                            order_id=order_id(order),
                            priority=self._order_priority(ctx, order),
                            reason=f"送 {item} 到 {consumer}")
                if not self._target_zone_available(pos, consumer, ctx, task):
                    continue
                if registry.can_claim(task):
                    return task
        return None

    def _choose_empty_vehicle_task(self, pos: list, ctx: dict,
                                   registry: ClaimRegistry) -> Optional[Task]:
        """空车：取成品/取原料 统一候选池，按订单贡献+距离折扣排序。"""
        candidates = []

        # ---- 候选：取成品（含delivery前瞻距离） ----
        for order in self._sorted_orders(ctx):
            product = order_product(order)
            order_score = self._order_priority(ctx, order)
            consumer = order_consumer(order)
            for zid in ctx["proc_zones"]:
                zone = ctx["zones"][zid]
                if product not in zone.get("outputs", []) or not zone.get("ready"):
                    continue
                task = Task(kind=TaskKind.PICK_PRODUCT, item=product, pick_zone=zid,
                            order_id=order_id(order), priority=order_score,
                            reason=f"取成品 {product}")
                if not self._target_zone_available(pos, zid, ctx, task):
                    continue
                if registry.can_claim(task):
                    pick_dist = self._zone_distance(pos, zid)
                    delivery_dist = self._lookahead_distance(zid, consumer)
                    score = (order_score
                             - pick_dist * self.PICK_DISTANCE_WEIGHT
                             - delivery_dist * self.DELIVERY_LOOKAHEAD_WEIGHT)
                    candidates.append((score, task))

        # ---- 候选：取原料（链接到订单价值 + 紧缺度） ----
        for order in self._sorted_orders(ctx):
            product = order_product(order)
            order_score = self._order_priority(ctx, order)
            for pzid in ctx["proc_zones"]:
                pz = ctx["zones"][pzid]
                if product not in pz.get("outputs", []):
                    continue
                for needed_item in pz.get("inputs", []):
                    if pz.get("items", {}).get(needed_item, 0) + registry.material_in_transit(pzid, needed_item) >= 1:
                        continue
                    for rzid in ctx["raw_zones"]:
                        rz = ctx["zones"][rzid]
                        if needed_item not in rz.get("outputs", []) or not rz.get("ready"):
                            continue
                        task = Task(kind=TaskKind.PICK_RAW, item=needed_item,
                                    pick_zone=rzid, priority=10.0,
                                    reason=f"取原料 {needed_item}")
                        if not self._target_zone_available(pos, rzid, ctx, task):
                            continue
                        if registry.can_claim(task):
                            dist = self._zone_distance(pos, rzid)
                            scarcity = self._material_scarcity(needed_item, ctx)
                            score = (order_score * 0.3
                                     + scarcity * 30.0
                                     - dist * self.PICK_DISTANCE_WEIGHT)
                            candidates.append((score, task))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
        return None


# ======================================================================
# V3：前馈补料 + 需求中断
# ======================================================================

class V3Strategy(V2Strategy):
    """V3: 全局贪心分配 + 前馈补料 + 任务中断。

    相对 V2 的改进：
    1. 全局贪心：所有空闲车辆候选统一打分、全局排序、贪心分配
       （V2 逐车贪心：v1 可能抢走 v2 更合适的任务）
    2. 前馈补料：分配后仍空闲的车辆，预取订单所需原料
    3. 任务中断：有订单需求时，中断低优先级前馈补料车辆
    """

    FORWARD_FILL_MAX_DISTANCE = 60.0
    FF_PICK_DISTANCE_WEIGHT = 0.5
    FORWARD_FILL_PRIORITY = 5.0

    # ================================================================
    # 全局贪心分配 + 中断
    # ================================================================

    def _compute_commands(self, ctx: dict) -> dict:
        registry = ClaimRegistry.from_memory(self.memory, ctx["vehicles"])
        self._augment_registry(registry, ctx)
        commands: dict = {}

        # Step 0: 有订单需求时中断前馈补料车辆
        if ctx["pending_orders"]:
            self._abandon_low_priority_tasks(ctx, commands)

        # Step 1: 收集所有空闲车辆及其候选
        idle_vehicles = []
        for vid in sorted(ctx["vehicles"], key=vehicle_sort_key):
            vehicle = ctx["vehicles"][vid]
            if vehicle.get("status") != "idle":
                continue
            candidates = self._gen_candidates(vehicle, ctx, registry)
            if candidates:
                idle_vehicles.append((vid, vehicle, candidates))

        if idle_vehicles:
            # Step 2: 构建全局打分对 (score, vehicle_index, task_key)
            scored_pairs = []
            task_pool: dict[tuple, Task] = {}
            for vi, (vid, vehicle, candidates) in enumerate(idle_vehicles):
                pos = vehicle.get("position")
                for task in candidates:
                    key = self._task_key(task)
                    if key not in task_pool:
                        task_pool[key] = task
                    dist = self._zone_distance(pos, task.target_zone or "")
                    # 沿用 V2 的距离折扣：score = task.priority - distance * weight
                    score = task.priority - dist * V2Strategy.PICK_DISTANCE_WEIGHT
                    scored_pairs.append((score, vi, key))

            # Step 3: 全局按得分降序排列
            scored_pairs.sort(key=lambda x: x[0], reverse=True)

            # Step 4: 贪心分配（已分配的车/已领取的任务跳过）
            assigned: set[int] = set()
            claimed: set[tuple] = set()
            for score, vi, key in scored_pairs:
                if vi in assigned or key in claimed:
                    continue
                task = task_pool[key]
                if not registry.can_claim(task):
                    continue
                vid, vehicle, _ = idle_vehicles[vi]
                if not self._target_zone_available(
                    vehicle.get("position"), task.target_zone or "", ctx, task
                ):
                    continue
                cmd = self._build_command(
                    vehicle, task, ctx, vehicle_id=vid, registry=registry
                )
                if not cmd:
                    continue
                registry.claim(task)
                self.memory.active_tasks[vid] = ActiveTask(
                    vehicle_id=vid, task=task, assigned_at=ctx["time"],
                    start_carrying=vehicle.get("carrying"),
                )
                commands[vid] = cmd
                assigned.add(vi)
                claimed.add(key)
                self.logger.log_command(ctx, vid, cmd, task=task, source="v3_greedy")

            # Step 5: 未分配车辆尝试前馈补料
            for vi, (vid, vehicle, _) in enumerate(idle_vehicles):
                if vi in assigned:
                    continue
                pos = vehicle.get("position")
                task = self._choose_forward_fill_task(pos, ctx, registry)
                if task and registry.can_claim(task):
                    cmd = self._build_command(vehicle, task, ctx,
                                              vehicle_id=vid, registry=registry)
                    if cmd:
                        registry.claim(task)
                        self.memory.active_tasks[vid] = ActiveTask(
                            vehicle_id=vid, task=task, assigned_at=ctx["time"],
                            start_carrying=vehicle.get("carrying"),
                        )
                        commands[vid] = cmd
                        self.logger.log_command(ctx, vid, cmd, task=task, source="v3_ff")

        return commands

    # ================================================================
    # 中断机制
    # ================================================================

    def _abandon_low_priority_tasks(self, ctx: dict, commands: dict) -> None:
        """中断未载货的前馈补料车辆，释放运力给需求任务。"""
        for vid, active in list(self.memory.active_tasks.items()):
            if not self._is_forward_fill(active.task):
                continue
            vehicle = ctx["vehicles"].get(vid)
            if not vehicle or vehicle.get("status") != "moving":
                continue
            if vehicle.get("carrying") is not None:
                continue  # 已载货不中断，避免浪费
            commands[vid] = {"path": [], "action": {"type": "abandon"}}
            self.memory.active_tasks.pop(vid, None)
            self.logger.log_command(ctx, vid, commands[vid],
                                   task=active.task, source="v3_interrupt")

    @staticmethod
    def _is_forward_fill(task: Task) -> bool:
        """低优先级（<15）或标记为前馈补料的任务可被中断。"""
        if task.priority < 15:
            return True
        return task.reason.startswith("FF:")

    # ================================================================
    # 候选生成（为每辆车生成所有可能的任务，不做 claim 检查）
    # ================================================================

    def _gen_candidates(self, vehicle: dict, ctx: dict,
                         registry: ClaimRegistry) -> list:
        """生成该车所有候选任务，存入 task.priority 作为基础优先级。"""
        carrying = vehicle.get("carrying")
        tasks = []

        if carrying and carrying in ctx["raw_items"]:
            # 持原料 → 送加工区（纯距离最优）
            for zid in ctx["proc_zones"]:
                z = ctx["zones"][zid]
                if carrying not in z.get("inputs", []):
                    continue
                if z.get("items", {}).get(carrying, 0) + registry.material_in_transit(zid, carrying) >= 1:
                    continue
                tasks.append(Task(kind=TaskKind.DROP_MATERIAL, item=carrying,
                                  drop_zone=zid, priority=20.0,
                                  reason=f"送 {carrying} 到 {zid}"))

        elif carrying:
            # 持成品 → 送消费区
            for o in ctx["pending_orders"]:
                if order_product(o) != carrying:
                    continue
                c = order_consumer(o)
                if not c or not ctx["zones"].get(c, {}).get("ready"):
                    continue
                base = self._order_priority(ctx, o)
                tasks.append(Task(kind=TaskKind.DROP_PRODUCT, item=carrying,
                                  drop_zone=c, order_id=order_id(o),
                                  priority=base, reason=f"送 {carrying} 到 {c}"))

        else:
            # 空车 → 取成品 + 取原料
            # 取成品（优先级含 delivery 前瞻距离）
            for o in ctx["pending_orders"]:
                p = order_product(o)
                order_score = self._order_priority(ctx, o)
                consumer = order_consumer(o)
                for zid in ctx["proc_zones"]:
                    z = ctx["zones"][zid]
                    if p not in z.get("outputs", []) or not z.get("ready"):
                        continue
                    delivery_dist = self._lookahead_distance(zid, consumer)
                    # 优先级预扣 delivery 距离（与车辆位置无关）
                    pri = order_score - delivery_dist * V2Strategy.DELIVERY_LOOKAHEAD_WEIGHT
                    tasks.append(Task(kind=TaskKind.PICK_PRODUCT, item=p,
                                      pick_zone=zid, order_id=order_id(o),
                                      priority=pri, reason=f"取成品 {p}"))
            # 取原料（链接到订单价值）
            for o in ctx["pending_orders"]:
                p = order_product(o)
                order_score = self._order_priority(ctx, o)
                for pzid in ctx["proc_zones"]:
                    pz = ctx["zones"][pzid]
                    if p not in pz.get("outputs", []):
                        continue
                    for item in pz.get("inputs", []):
                        if pz.get("items", {}).get(item, 0) + registry.material_in_transit(pzid, item) >= 1:
                            continue
                        for rzid in ctx["raw_zones"]:
                            rz = ctx["zones"][rzid]
                            if item not in rz.get("outputs", []) or not rz.get("ready"):
                                continue
                            scarcity = self._material_scarcity(item, ctx)
                            base = order_score * 0.5 + scarcity * 10.0
                            tasks.append(Task(kind=TaskKind.PICK_RAW, item=item,
                                              pick_zone=rzid, priority=base,
                                              reason=f"取原料 {item}"))
        return tasks

    @staticmethod
    def _task_key(task: Task) -> tuple:
        return (task.kind.value, task.item, task.pick_zone, task.drop_zone, task.order_id)

    # ================================================================
    # 前馈补料（低优先级，可中断）
    # ================================================================

    def _choose_forward_fill_task(self, pos, ctx, registry):
        """空闲车辆预取订单所需原料，帮助加工区备料。"""
        ordered = {order_product(o) for o in ctx["pending_orders"]}
        if not ordered:
            return None
        cand = []
        for pzid in ctx["proc_zones"]:
            pz = ctx["zones"][pzid]
            if not (set(pz.get("outputs", [])) & ordered):
                continue
            for item in pz.get("inputs", []):
                if pz.get("items", {}).get(item, 0) + registry.material_in_transit(pzid, item) >= 1:
                    continue
                for rzid in ctx["raw_zones"]:
                    rz = ctx["zones"][rzid]
                    if item not in rz.get("outputs", []) or not rz.get("ready"):
                        continue
                    pri = self.FORWARD_FILL_PRIORITY + self._material_scarcity(item, ctx)
                    task = Task(kind=TaskKind.PICK_RAW, item=item, pick_zone=rzid,
                                priority=pri, reason=f"FF:取 {item}")
                    if registry.can_claim(task):
                        dist = self._zone_distance(pos, rzid)
                        if dist > self.FORWARD_FILL_MAX_DISTANCE:
                            continue
                        cand.append((pri - dist * self.FF_PICK_DISTANCE_WEIGHT, task))
        if cand:
            cand.sort(key=lambda x: x[0], reverse=True)
            return cand[0][1]
        return None


class V3_1Strategy(V3Strategy):
    """V3_1: V3 + 链完成增强 + 拥堵感知分配。

    相对 V3 的改进：
    1. 链完成倍率：原料优先级 × 链完成度倍率（完成度越高越优先）
    2. 拥堵感知：车辆评分时轻微惩罚同一目标区的已有任务
       （-10pts/每车已前往该区），仅用于平局决胜
    3. 最后一块奖励：当原料是加工链的最后一个缺口时，额外 +50% 基础分
    """

    CHAIN_LAST_PIECE_BONUS = 1.5
    CONGESTION_PENALTY = 10.0
    FORWARD_FILL_PRIORITY = 5.0

    # ================================================================
    # 链完成倍率覆写
    # ================================================================

    def _gen_candidates(self, vehicle: dict, ctx: dict,
                         registry: ClaimRegistry) -> list:
        """生成候选 + 链完成倍率调整优先级。"""
        tasks = super()._gen_candidates(vehicle, ctx, registry)
        for task in tasks:
            if task.kind in (TaskKind.DROP_MATERIAL, TaskKind.PICK_RAW):
                multiplier = self._chain_completion_multiplier(task.item, ctx, registry)
                task.priority *= multiplier
        return tasks

    def _chain_completion_multiplier(self, item: str, ctx: dict,
                                      registry: ClaimRegistry = None) -> float:
        """链完成度越高倍率越大，最后一块有额外奖励。

        例：4 种原料已完成 3 种 → ratio=0.75 → 1 + 0.75*2 = 2.5x
            且 missing==1 → +1.5 → 最终 4.0x
        """
        registry = registry or ClaimRegistry()
        best = 1.0
        ordered_products = {order_product(o) for o in ctx["pending_orders"]}
        for pzid in ctx["proc_zones"]:
            pz = ctx["zones"][pzid]
            if item not in pz.get("inputs", []):
                continue
            if not (set(pz.get("outputs", [])) & ordered_products):
                continue
            current = pz.get("items", {}).get(item, 0)
            in_transit = registry.material_in_transit(pzid, item)
            if current + in_transit >= 1:
                continue
            inputs = pz.get("inputs", [])
            have = sum(1 for inp in inputs
                       if pz.get("items", {}).get(inp, 0) +
                          registry.material_in_transit(pzid, inp) >= 1)
            missing = len(inputs) - have
            ratio = have / len(inputs) if inputs else 0
            multiplier = 1.0 + ratio * 2.0
            if missing == 1:
                multiplier += self.CHAIN_LAST_PIECE_BONUS
            if multiplier > best:
                best = multiplier
        return best

    # ================================================================
    # 拥堵感知分配
    # ================================================================

    def _zone_congestion(self, zone_id: str) -> int:
        """统计当前正在前往该区的车辆数。"""
        if not zone_id:
            return 0
        count = 0
        for active in self.memory.active_tasks.values():
            if active.task.target_zone == zone_id:
                count += 1
        return count

    def _compute_commands(self, ctx: dict) -> dict:
        """V3 全局贪心 + 拥堵感知轻微惩罚（平局决胜）。"""
        registry = ClaimRegistry.from_memory(self.memory, ctx["vehicles"])
        self._augment_registry(registry, ctx)
        commands: dict = {}

        if ctx["pending_orders"]:
            self._abandon_low_priority_tasks(ctx, commands)

        idle_vehicles = []
        for vid in sorted(ctx["vehicles"], key=vehicle_sort_key):
            vehicle = ctx["vehicles"][vid]
            if vehicle.get("status") != "idle":
                continue
            candidates = self._gen_candidates(vehicle, ctx, registry)
            if candidates:
                idle_vehicles.append((vid, vehicle, candidates))

        if idle_vehicles:
            scored_pairs = []
            task_pool: dict[tuple, Task] = {}
            for vi, (vid, vehicle, candidates) in enumerate(idle_vehicles):
                pos = vehicle.get("position")
                for task in candidates:
                    key = self._task_key(task)
                    if key not in task_pool:
                        task_pool[key] = task
                    dist = self._zone_distance(pos, task.target_zone or "")
                    # 拥堵惩罚：每个已前往该区的车 -10 分
                    congestion = self._zone_congestion(task.target_zone or "")
                    score = (task.priority
                             - dist * V2Strategy.PICK_DISTANCE_WEIGHT
                             - congestion * self.CONGESTION_PENALTY)
                    scored_pairs.append((score, vi, key))

            scored_pairs.sort(key=lambda x: x[0], reverse=True)
            assigned: set[int] = set()
            claimed: set[tuple] = set()
            for score, vi, key in scored_pairs:
                if vi in assigned or key in claimed:
                    continue
                task = task_pool[key]
                if not registry.can_claim(task):
                    continue
                vid, vehicle, _ = idle_vehicles[vi]
                if not self._target_zone_available(
                    vehicle.get("position"), task.target_zone or "", ctx, task
                ):
                    continue
                cmd = self._build_command(vehicle, task, ctx,
                                          vehicle_id=vid, registry=registry)
                if not cmd:
                    continue
                registry.claim(task)
                self.memory.active_tasks[vid] = ActiveTask(
                    vehicle_id=vid, task=task, assigned_at=ctx["time"],
                    start_carrying=vehicle.get("carrying"),
                )
                commands[vid] = cmd
                assigned.add(vi)
                claimed.add(key)
                self.logger.log_command(ctx, vid, cmd, task=task, source="v31_greedy")

            # 未分配车辆前馈补料
            for vi, (vid, vehicle, _) in enumerate(idle_vehicles):
                if vi in assigned:
                    continue
                pos = vehicle.get("position")
                task = V3Strategy._choose_forward_fill_task(self, pos, ctx, registry)
                if task and registry.can_claim(task):
                    cmd = self._build_command(vehicle, task, ctx,
                                              vehicle_id=vid, registry=registry)
                    if cmd:
                        registry.claim(task)
                        self.memory.active_tasks[vid] = ActiveTask(
                            vehicle_id=vid, task=task, assigned_at=ctx["time"],
                            start_carrying=vehicle.get("carrying"),
                        )
                        commands[vid] = cmd
                        self.logger.log_command(ctx, vid, cmd, task=task, source="v31_ff")

        return commands


class V3_2Strategy(V3_1Strategy):
    """V3_2: V3_1 + 匈牙利全局最优匹配。

    当空闲车辆数 × 统一任务池大小 ≤ HUNGARIAN_MAX_OPS 时，
    使用匈牙利算法求全局最优匹配（而非贪心）。

    匈牙利保证：不会有"v1 抢了 v2 更合适的任务"。
    适用场景：空闲车辆少、任务候选少的中后期（前期任务多时自动退化为贪心）。
    """

    HUNGARIAN_MAX_OPS = 300  # n*m ≤ 300 时启用匈牙利（约 10车×30任务）

    def _compute_commands(self, ctx: dict) -> dict:
        registry = ClaimRegistry.from_memory(self.memory, ctx["vehicles"])
        self._augment_registry(registry, ctx)
        commands: dict = {}

        if ctx["pending_orders"]:
            self._abandon_low_priority_tasks(ctx, commands)

        idle_vehicles = []
        for vid in sorted(ctx["vehicles"], key=vehicle_sort_key):
            vehicle = ctx["vehicles"][vid]
            if vehicle.get("status") != "idle":
                continue
            candidates = self._gen_candidates(vehicle, ctx, registry)
            if candidates:
                idle_vehicles.append((vid, vehicle, candidates))

        if idle_vehicles:
            # 构建统一任务池
            task_pool: dict[tuple, Task] = {}
            for _vid, _vehicle, candidates in idle_vehicles:
                for task in candidates:
                    key = self._task_key(task)
                    if key not in task_pool:
                        task_pool[key] = task

            task_list = list(task_pool.values())
            n_vehicles = len(idle_vehicles)
            n_tasks = len(task_list)
            ops = n_vehicles * n_tasks

            if ops <= self.HUNGARIAN_MAX_OPS and n_tasks > 0:
                assigned = self._hungarian_assign(
                    idle_vehicles, task_list, task_pool, ctx, registry, commands
                )
            else:
                assigned = set()

            # 贪心分配（匈牙利未启用的车，或匈牙利回退）
            if len(assigned) < n_vehicles:
                self._greedy_assign(
                    idle_vehicles, task_pool, ctx, registry, commands, assigned
                )

            # 未分配车辆前馈补料
            for vi, (vid, vehicle, _) in enumerate(idle_vehicles):
                if vi in assigned:
                    continue
                pos = vehicle.get("position")
                task = V3Strategy._choose_forward_fill_task(self, pos, ctx, registry)
                if task and registry.can_claim(task):
                    cmd = self._build_command(vehicle, task, ctx,
                                              vehicle_id=vid, registry=registry)
                    if cmd:
                        registry.claim(task)
                        self.memory.active_tasks[vid] = ActiveTask(
                            vehicle_id=vid, task=task, assigned_at=ctx["time"],
                            start_carrying=vehicle.get("carrying"),
                        )
                        commands[vid] = cmd
                        self.logger.log_command(ctx, vid, cmd, task=task, source="v32_ff")

        return commands

    # ================================================================
    # 匈牙利全局最优匹配
    # ================================================================

    def _hungarian_assign(self, idle_vehicles, task_list, task_pool,
                           ctx, registry, commands) -> set:
        """匈牙利算法全局最优匹配。返回已分配的 vehicle indices。"""
        n = len(idle_vehicles)
        m = len(task_list)
        INF = 1e9

        # 构建代价矩阵（n × m）：cost = -score
        cost = [[INF] * m for _ in range(n)]
        for i, (vid, vehicle, candidates) in enumerate(idle_vehicles):
            pos = vehicle.get("position")
            candidate_keys = {self._task_key(t) for t in candidates}
            for j, task in enumerate(task_list):
                key = self._task_key(task)
                if key not in candidate_keys:
                    continue
                dist = self._zone_distance(pos, task.target_zone or "")
                congestion = self._zone_congestion(task.target_zone or "")
                score = (task.priority
                         - dist * V2Strategy.PICK_DISTANCE_WEIGHT
                         - congestion * V3_1Strategy.CONGESTION_PENALTY)
                cost[i][j] = -score  # 匈牙利求最小代价，所以取负

        # 匈牙利算法
        assignment = hungarian_assign(cost)

        assigned = set()
        for i, j in enumerate(assignment):
            if j < 0 or j >= m:
                continue
            task = task_list[j]
            key = self._task_key(task)
            if not registry.can_claim(task):
                continue
            vid, vehicle, _ = idle_vehicles[i]
            if not self._target_zone_available(
                vehicle.get("position"), task.target_zone or "", ctx, task
            ):
                continue
            # 只接受正收益的分配
            dist = self._zone_distance(vehicle.get("position"), task.target_zone or "")
            score = task.priority - dist * V2Strategy.PICK_DISTANCE_WEIGHT
            if score < 0:
                continue
            cmd = self._build_command(vehicle, task, ctx, vehicle_id=vid, registry=registry)
            if not cmd:
                continue
            registry.claim(task)
            self.memory.active_tasks[vid] = ActiveTask(
                vehicle_id=vid, task=task, assigned_at=ctx["time"],
                start_carrying=vehicle.get("carrying"),
            )
            commands[vid] = cmd
            assigned.add(i)
            self.logger.log_command(ctx, vid, cmd, task=task, source="v32_hungarian")

        return assigned

    # ================================================================
    # 贪心回退
    # ================================================================

    def _greedy_assign(self, idle_vehicles, task_pool, ctx, registry, commands, assigned):
        """V3_1 风格的贪心分配（用于匈牙利未覆盖的车辆）。"""
        scored_pairs = []
        for vi, (vid, vehicle, candidates) in enumerate(idle_vehicles):
            if vi in assigned:
                continue
            pos = vehicle.get("position")
            for task in candidates:
                key = self._task_key(task)
                dist = self._zone_distance(pos, task.target_zone or "")
                congestion = self._zone_congestion(task.target_zone or "")
                score = (task.priority
                         - dist * V2Strategy.PICK_DISTANCE_WEIGHT
                         - congestion * V3_1Strategy.CONGESTION_PENALTY)
                scored_pairs.append((score, vi, key))

        scored_pairs.sort(key=lambda x: x[0], reverse=True)
        claimed = set()
        for score, vi, key in scored_pairs:
            if vi in assigned or key in claimed:
                continue
            task = task_pool[key]
            if not registry.can_claim(task):
                continue
            vid, vehicle, _ = idle_vehicles[vi]
            if not self._target_zone_available(
                vehicle.get("position"), task.target_zone or "", ctx, task
            ):
                continue
            cmd = self._build_command(vehicle, task, ctx, vehicle_id=vid, registry=registry)
            if not cmd:
                continue
            registry.claim(task)
            self.memory.active_tasks[vid] = ActiveTask(
                vehicle_id=vid, task=task, assigned_at=ctx["time"],
                start_carrying=vehicle.get("carrying"),
            )
            commands[vid] = cmd
            assigned.add(vi)
            claimed.add(key)
            self.logger.log_command(ctx, vid, cmd, task=task, source="v32_greedy")


class V4Strategy(V2Strategy):
    """V4：检测近距离车辆，低收益车避让重规划。"""

    COLLISION_WARN_DISTANCE = 3.5
    IDLE_CLEAR_DISTANCE = 2.0
    CLEARANCE_SPEED = 12.0
    REPLAN_COOLDOWN_SECONDS = 1.0
    CLEARANCE_COOLDOWN_SECONDS = 0.4
    EXPECTED_GAIN_WEIGHT = 0.03
    RANDOM_REPLAN_PROB = 0.15
    BLOCK_LOOKAHEAD = 3
    V4_EVENT_LOG_INTERVAL = 1.0
    DEFAULT_RAW_REWARD = 30.0
    DEFAULT_PRODUCT_REWARD = 100.0
    CONGESTION_CURRENT_PENALTY = 60.0
    CONGESTION_NEXT_PENALTY = 35.0
    CONGESTION_FUTURE_PENALTY = 18.0
    CONGESTION_TARGET_PENALTY = 25.0
    EMERGENCY_KEEPER_STATUS_BONUS = 8.0

    def __init__(self, sdk) -> None:
        super().__init__(sdk)
        self.random_replan_enabled = (
            os.environ.get("STRATEGY_RANDOM_REPLAN", "0").lower()
            in {"1", "true", "yes", "on"}
        )
        self.congestion_path_enabled = (
            os.environ.get("V4_CONGESTION_PATH", "0").lower()
            in {"1", "true", "yes", "on"}
        )
        self.CLEARANCE_SPEED = self._env_float("V4_CLEARANCE_SPEED", self.CLEARANCE_SPEED)
        self.REPLAN_COOLDOWN_SECONDS = self._env_float(
            "V4_REPLAN_COOLDOWN", self.REPLAN_COOLDOWN_SECONDS
        )
        self.COLLISION_WARN_DISTANCE = self._env_float(
            "V4_WARN_DISTANCE", self.COLLISION_WARN_DISTANCE
        )

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default

    def __call__(self, state: dict) -> dict:
        if self._handle_terminal_state(state):
            return {}
        ctx = self._prepare(state)
        self.memory.prune(ctx["vehicles"], ctx["time"], self.STALE_TASK_SECONDS)
        self.memory.reserved_clearance_nodes.clear()
        commands = self._compute_commands(ctx)
        commands.update(self._build_replan_overrides(ctx, commands))
        self._update_last_nodes(ctx)
        self.logger.log_snapshot(state, self.memory)
        return commands

    def _build_command(self, vehicle: dict, task: Task,
                       ctx: dict = None, vehicle_id: str = None,
                       registry: ClaimRegistry = None) -> Optional[dict]:
        if task.kind in {TaskKind.WAIT, TaskKind.ABANDON}:
            return super()._build_command(vehicle, task, ctx, vehicle_id, registry)
        if not self.congestion_path_enabled:
            return super()._build_command(vehicle, task, ctx, vehicle_id, registry)

        target_zone = task.target_zone
        action_type = task.action_type
        if not target_zone or not action_type or not self.sdk or not ctx:
            return super()._build_command(vehicle, task, ctx, vehicle_id, registry)

        start_pos = vehicle.get("position")
        start = self._node_from_position(start_pos)
        end = self.sdk.get_zone_node(target_zone)
        if not start or not end:
            return super()._build_command(vehicle, task, ctx, vehicle_id, registry)

        penalties = self._congestion_penalties(ctx, vehicle_id, end)
        path = self.sdk.plan_path_with_penalty(start, end, penalties)
        if not path:
            return super()._build_command(vehicle, task, ctx, vehicle_id, registry)

        command = {
            "path": self.sdk.nodes_to_points(path),
            "action": {"type": action_type, "target_zone": target_zone},
            "speed": self.CRUISE_SPEED,
        }
        self.logger.log_event_throttled(
            "v4_congestion_path",
            key=(vehicle_id, target_zone, tuple(path[:4]), tuple(sorted(penalties.items())[:6])),
            min_interval=self.V4_EVENT_LOG_INTERVAL,
            state_or_ctx=ctx,
            vehicle_id=vehicle_id,
            target_zone=target_zone,
            penalty_nodes=len(penalties),
            path_distance=round(self.sdk.path_distance(path), 3),
        )
        return command

    def _validate_task(self, task: Optional[Task], vehicle: dict,
                       ctx: dict) -> Optional[Task]:
        if not task:
            return None
        zone = ctx["zones"].get(task.target_zone or "")
        if not zone:
            return None
        if task.kind == TaskKind.PICK_RAW:
            if not zone.get("ready") or zone.get("items", {}).get(task.item, 0) <= 0:
                return None
        elif task.kind == TaskKind.PICK_PRODUCT:
            if not zone.get("ready"):
                return None
        elif task.kind == TaskKind.DROP_MATERIAL:
            if task.item not in zone.get("inputs", []):
                return None
            if zone.get("items", {}).get(task.item, 0) >= 1:
                return None
        elif task.kind == TaskKind.DROP_PRODUCT:
            if not self._has_pending_order_for(task.item or "", task.drop_zone or "", ctx):
                return None
        return task

    def _build_replan_overrides(self, ctx: dict, base_commands: dict | None = None) -> dict:
        if not self.sdk or len(ctx["vehicles"]) < 2:
            return {}

        base_commands = base_commands or {}
        cache = self._collect_vehicle_cache(ctx)
        if len(cache) < 2:
            return {}

        expected_gains = {
            vid: self._expected_gain(vid, ctx, cache)
            for vid in cache
        }

        replanned: set[str] = set()
        overrides = self._build_emergency_separation(ctx, cache, expected_gains)
        replanned.update(overrides)
        vids = sorted(cache.keys(), key=vehicle_sort_key)

        for i in range(len(vids)):
            for j in range(i + 1, len(vids)):
                a, b = vids[i], vids[j]
                if a in replanned or b in replanned:
                    continue
                if self._distance(cache[a]["pos"], cache[b]["pos"]) > self.COLLISION_WARN_DISTANCE:
                    continue

                direction = self._direction_relation(a, b, cache)
                cooldown_age_a = self._cooldown_age(a, ctx)
                cooldown_age_b = self._cooldown_age(b, ctx)
                self.logger.log_event_throttled(
                    "v4_close_pair",
                    key=(self._pair_key(a, b), direction),
                    min_interval=self.V4_EVENT_LOG_INTERVAL,
                    state_or_ctx=ctx,
                    a=a,
                    b=b,
                    distance=round(self._distance(cache[a]["pos"], cache[b]["pos"]), 3),
                    expected_gain_a=round(expected_gains[a], 3),
                    expected_gain_b=round(expected_gains[b], 3),
                    cooldown_age_a=cooldown_age_a,
                    cooldown_age_b=cooldown_age_b,
                    in_cooldown_a=cooldown_age_a < self.REPLAN_COOLDOWN_SECONDS,
                    in_cooldown_b=cooldown_age_b < self.REPLAN_COOLDOWN_SECONDS,
                    direction=direction,
                    current_node_a=cache[a]["current_node"],
                    current_node_b=cache[b]["current_node"],
                    next_node_a=cache[a]["next_node"],
                    next_node_b=cache[b]["next_node"],
                )

                if self._distance(cache[a]["pos"], cache[b]["pos"]) <= self.IDLE_CLEAR_DISTANCE:
                    clearance_vid = self._choose_idle_clearance_vehicle(
                        a, b, ctx, cache, expected_gains, base_commands
                    )
                    if clearance_vid:
                        other = b if clearance_vid == a else a
                        cmd = self._clearance_vehicle(clearance_vid, other, ctx, cache)
                        if cmd:
                            overrides[clearance_vid] = cmd
                            replanned.add(clearance_vid)
                            self.memory.last_replan_time[clearance_vid] = ctx["time"]
                            continue

                if direction == "same_direction":
                    follower = self._choose_replanner(a, b, ctx, expected_gains, cache)
                    if follower and follower not in replanned:
                        active = self.memory.active_tasks.get(follower)
                        if active:
                            cmd = self._speed_stagger_vehicle(follower, b if follower == a else a, ctx, active.task)
                            if cmd:
                                overrides[follower] = cmd
                                replanned.add(follower)
                                self.memory.last_replan_time[follower] = ctx["time"]
                                continue

                replanner = self._choose_replanner(a, b, ctx, expected_gains, cache)
                if not replanner:
                    self._log_replan_skip(
                        "v4_replan_skip",
                        ctx,
                        a=a,
                        b=b,
                        reason="both_or_chosen_vehicle_in_cooldown",
                    )
                    continue
                other = b if replanner == a else a

                cmd = self._replan_vehicle(replanner, other, ctx, cache)
                if cmd:
                    overrides[replanner] = cmd
                    replanned.add(replanner)
                    self.memory.last_replan_time[replanner] = ctx["time"]

        return overrides

    def _build_emergency_separation(self, ctx: dict, cache: dict[str, dict],
                                    expected_gains: dict[str, float]) -> dict[str, dict]:
        groups = self._danger_groups(cache)
        if not groups:
            return {}

        overrides: dict[str, dict] = {}
        reserved = set(self.memory.reserved_clearance_nodes)
        occupied_targets = self._occupied_target_nodes(ctx)

        for group in groups:
            keeper = self._choose_emergency_keeper(group, ctx, expected_gains)
            cleared: list[str] = []
            reserved_before = set(reserved)
            for vid in sorted(group, key=vehicle_sort_key):
                if vid == keeper:
                    continue
                target = self._safe_neighbor_node(
                    vid, ctx, cache,
                    reserved_nodes=reserved,
                    avoid_nodes=occupied_targets,
                )
                if not target:
                    target = self._safe_neighbor_node(
                        vid, ctx, cache,
                        reserved_nodes=reserved,
                    )
                if not target:
                    continue
                cmd = self._clearance_vehicle(vid, keeper, ctx, cache, target_node=target)
                if not cmd:
                    continue
                overrides[vid] = cmd
                reserved.add(target)
                self.memory.reserved_clearance_nodes.add(target)
                self.memory.last_clearance_time[vid] = ctx["time"]
                self.memory.last_replan_time[vid] = ctx["time"]
                cleared.append(vid)

            if cleared:
                self.logger.log_event_throttled(
                    "v4_emergency_separation",
                    key=(tuple(sorted(group, key=vehicle_sort_key)), keeper, tuple(cleared)),
                    min_interval=0.5,
                    state_or_ctx=ctx,
                    group=sorted(group, key=vehicle_sort_key),
                    keeper=keeper,
                    cleared=cleared,
                    reserved_nodes=sorted(reserved - reserved_before),
                )

        return overrides

    def _danger_groups(self, cache: dict[str, dict]) -> list[set[str]]:
        vids = sorted(cache, key=vehicle_sort_key)
        groups: list[set[str]] = []
        for vid in vids:
            merged = {vid}
            for group in groups:
                if any(self._is_danger_pair(vid, other, cache) for other in group):
                    group.add(vid)
                    merged = group
                    break
            if merged == {vid}:
                groups.append(merged)

        # Merge transitive groups that became connected after insertion.
        changed = True
        while changed:
            changed = False
            next_groups: list[set[str]] = []
            for group in groups:
                for existing in next_groups:
                    if any(self._is_danger_pair(a, b, cache) for a in group for b in existing):
                        existing.update(group)
                        changed = True
                        break
                else:
                    next_groups.append(set(group))
            groups = next_groups

        return [g for g in groups if len(g) > 1]

    def _is_danger_pair(self, a: str, b: str, cache: dict[str, dict]) -> bool:
        dist = self._distance(cache[a]["pos"], cache[b]["pos"])
        return dist < self.IDLE_CLEAR_DISTANCE

    def _choose_emergency_keeper(self, group: set[str], ctx: dict,
                                 expected_gains: dict[str, float]) -> str:
        def key(vid: str) -> tuple[float, int, int, int]:
            vehicle = ctx["vehicles"].get(vid, {})
            carrying = vehicle.get("carrying")
            active = self.memory.active_tasks.get(vid)
            task = active.task if active else None
            status_bonus = self.EMERGENCY_KEEPER_STATUS_BONUS if vehicle.get("status") == "moving" else 0.0
            carrying_bonus = 6.0 if carrying in ctx.get("prod_items", set()) else (3.0 if carrying else 0.0)
            task_bonus = 2.0 if task and task.kind in {TaskKind.DROP_PRODUCT, TaskKind.PICK_PRODUCT} else 0.0
            sort_key = vehicle_sort_key(vid)[0]
            return (
                expected_gains.get(vid, 0.0) + status_bonus + carrying_bonus + task_bonus,
                1 if carrying else 0,
                1 if vehicle.get("status") == "moving" else 0,
                -sort_key,
            )

        return max(group, key=key)

    def _collect_vehicle_cache(self, ctx: dict) -> dict[str, dict]:
        cache: dict[str, dict] = {}
        for vid, v in ctx["vehicles"].items():
            pos = v.get("position")
            if not pos:
                continue
            current_node = self._node_from_position(pos)
            next_node = self._preview_next_node(v, current_node)
            cache[vid] = {
                "pos": pos,
                "current_node": current_node,
                "next_node": next_node,
            }
        return cache

    def _node_from_position(self, pos: list | None) -> Optional[str]:
        if not pos or not self.sdk:
            return None
        return self.sdk.find_nearest_node(pos[0], pos[1])

    def _congestion_penalties(self, ctx: dict, vehicle_id: str | None,
                              end_node: str | None) -> dict[str, float]:
        penalties: dict[str, float] = {}
        if not self.sdk:
            return penalties

        for other, vehicle in ctx["vehicles"].items():
            if other == vehicle_id:
                continue
            current = self._node_from_position(vehicle.get("position"))
            if current and current != end_node:
                penalties[current] = penalties.get(current, 0.0) + self.CONGESTION_CURRENT_PENALTY

            future_seen = set()
            for idx, point in enumerate(vehicle.get("path_preview", [])[: self.BLOCK_LOOKAHEAD + 1]):
                node = self._node_from_position(point)
                if not node or node == current or node == end_node or node in future_seen:
                    continue
                future_seen.add(node)
                penalty = self.CONGESTION_NEXT_PENALTY if idx == 0 else self.CONGESTION_FUTURE_PENALTY
                penalties[node] = penalties.get(node, 0.0) + penalty

        for active in self.memory.active_tasks.values():
            if active.vehicle_id == vehicle_id:
                continue
            target_zone = active.task.target_zone
            target_node = self.sdk.get_zone_node(target_zone) if target_zone else None
            if target_node and target_node != end_node:
                penalties[target_node] = penalties.get(target_node, 0.0) + self.CONGESTION_TARGET_PENALTY

        return penalties

    def _occupied_target_nodes(self, ctx: dict) -> set[str]:
        nodes = set()
        if not self.sdk:
            return nodes
        for active in self.memory.active_tasks.values():
            target_zone = active.task.target_zone
            node = self.sdk.get_zone_node(target_zone) if target_zone else None
            if node:
                nodes.add(node)
        return nodes

    def _has_pending_order_for(self, item: str, consumer: str, ctx: dict) -> bool:
        for order in ctx.get("pending_orders", []):
            if order_product(order) == item and order_consumer(order) == consumer:
                return True
        return False

    def _target_zone_available(self, pos: list, zone_id: str, ctx: dict,
                               task: Task = None) -> bool:
        if os.environ.get("V4_PHYSICAL_ZONE_OCCUPANCY", "0").lower() not in {"1", "true", "yes", "on"}:
            return True
        zone = ctx.get("zones", {}).get(zone_id, {})
        if zone.get("type") != "processing":
            return True
        zone_pos = self.sdk.get_zone_position(zone_id) if self.sdk else None
        if not pos or not zone_pos:
            return True
        # If this vehicle is already at the target, let the interaction happen;
        # emergency separation handles any other vehicle sharing the spot.
        if self._distance(pos, zone_pos) <= self.IDLE_CLEAR_DISTANCE:
            return True

        target_node = self.sdk.get_zone_node(zone_id)
        for vehicle in ctx.get("vehicles", {}).values():
            other_pos = vehicle.get("position")
            if not other_pos:
                continue
            if vehicle.get("status") != "idle":
                continue
            if self._distance(other_pos, pos) <= 0.5:
                continue
            other_node = self._node_from_position(other_pos)
            if other_node == target_node and self._distance(other_pos, zone_pos) <= self.IDLE_CLEAR_DISTANCE:
                return False
            if self._distance(other_pos, zone_pos) <= self.IDLE_CLEAR_DISTANCE:
                return False
        return True

    def _preview_next_node(self, vehicle: dict, current_node: Optional[str]) -> Optional[str]:
        preview = vehicle.get("path_preview", [])
        for point in preview:
            node = self.sdk.find_nearest_node(point[0], point[1])
            if node and node != current_node:
                return node
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
                          expected_gains: dict[str, float],
                          cache: dict[str, dict]) -> Optional[str]:
        candidates = []
        for vid in (a, b):
            vehicle = ctx["vehicles"].get(vid, {})
            if vehicle.get("status") != "moving":
                continue
            if vid not in self.memory.active_tasks:
                continue
            if self._in_cooldown(vid, ctx):
                continue
            if cache.get(vid, {}).get("current_node") == cache.get(vid, {}).get("next_node"):
                continue
            candidates.append(vid)

        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        if self.random_replan_enabled and random.random() < self.RANDOM_REPLAN_PROB:
            return random.choice(candidates)
        return min(candidates, key=lambda vid: (expected_gains[vid], vid))

    def _choose_idle_clearance_vehicle(self, a: str, b: str, ctx: dict,
                                       cache: dict[str, dict],
                                       expected_gains: dict[str, float],
                                       base_commands: dict) -> Optional[str]:
        candidates = []
        for vid in (a, b):
            vehicle = ctx["vehicles"].get(vid, {})
            if vehicle.get("status") != "idle":
                continue
            if self._in_cooldown(vid, ctx):
                continue
            if not self._safe_neighbor_node(vid, ctx, cache):
                continue
            has_task = 1 if vid in self.memory.active_tasks else 0
            candidates.append((has_task, expected_gains.get(vid, 0.0), vid))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][2]

    def _command_moves_vehicle(self, command: dict) -> bool:
        path = command.get("path") or []
        return len(path) > 1 and self.sdk.points_distance(path) > self.IDLE_CLEAR_DISTANCE

    def _clearance_vehicle(self, vid: str, other: str, ctx: dict,
                           cache: dict[str, dict],
                           target_node: Optional[str] = None) -> Optional[dict]:
        start = cache.get(vid, {}).get("current_node")
        target = target_node or self._safe_neighbor_node(vid, ctx, cache)
        if not start or not target:
            self._log_replan_skip(
                "v4_replan_skip",
                ctx,
                vehicle_id=vid,
                other_id=other,
                reason="no_clearance_neighbor",
            )
            return None

        path = [start, target]
        command = {
            "path": self.sdk.nodes_to_points(path),
            "action": None,
            "speed": self.CLEARANCE_SPEED,
        }
        self.logger.log_event_throttled(
            "v4_replan",
            key=("idle_clearance", vid, other, start, target),
            min_interval=0.5,
            state_or_ctx=ctx,
            vehicle_id=vid,
            other_id=other,
            target_zone=None,
            action_type=None,
            mode="idle_clearance",
            fallback=False,
            blocked_nodes=[],
            start=start,
            end=target,
            path_nodes=path,
            path_distance=round(self.sdk.path_distance(path), 3),
        )
        self.logger.log_command(ctx, vid, command, source="v4_idle_clearance")
        self.memory.active_tasks.pop(vid, None)
        return command

    def _replan_vehicle(self, vid: str, other: str, ctx: dict,
                        cache: dict[str, dict]) -> Optional[dict]:
        vehicle = ctx["vehicles"].get(vid, {})
        if vehicle.get("status") != "moving":
            self._log_replan_skip(
                "v4_replan_skip",
                ctx,
                vehicle_id=vid,
                other_id=other,
                reason="vehicle_not_moving",
                status=vehicle.get("status"),
            )
            return None

        active = self.memory.active_tasks.get(vid)
        if not active:
            self._log_replan_skip(
                "v4_replan_skip",
                ctx,
                vehicle_id=vid,
                other_id=other,
                reason="no_active_task",
            )
            return None

        task = active.task
        target_zone = task.target_zone
        action_type = task.action_type
        if not target_zone or not action_type:
            self._log_replan_skip(
                "v4_replan_skip",
                ctx,
                vehicle_id=vid,
                other_id=other,
                reason="task_has_no_target_or_action",
                task_kind=task.kind,
            )
            return None

        start = cache[vid]["current_node"]
        end = self.sdk.get_zone_node(target_zone)
        if not start or not end:
            self._log_replan_skip(
                "v4_replan_skip",
                ctx,
                vehicle_id=vid,
                other_id=other,
                reason="missing_start_or_end_node",
                start=start,
                end=end,
                target_zone=target_zone,
            )
            return None
        if start == end:
            self._log_replan_skip(
                "v4_replan_skip",
                ctx,
                vehicle_id=vid,
                other_id=other,
                reason="start_equals_target",
                start=start,
                end=end,
                target_zone=target_zone,
            )
            return None

        blocked = self._build_blocked_nodes(vid, other, ctx, cache)
        blocked.discard(start)
        blocked.discard(end)
        if not blocked:
            self._log_replan_skip(
                "v4_replan_skip",
                ctx,
                vehicle_id=vid,
                other_id=other,
                reason="empty_blocked_nodes",
                start=start,
                end=end,
                target_zone=target_zone,
            )
            return self._speed_stagger_vehicle(vid, other, ctx, task)

        path = self.sdk.plan_path_with_blocked(start, end, blocked)
        if path and len(path) > 1:
            command = {
                "path": self.sdk.nodes_to_points(path),
                "action": {"type": action_type, "target_zone": target_zone},
                "speed": self.CRUISE_SPEED,
            }
            self.logger.log_event(
                "v4_replan",
                ctx,
                vehicle_id=vid,
                other_id=other,
                target_zone=target_zone,
                action_type=action_type,
                fallback=False,
                blocked_nodes=sorted(blocked),
                start=start,
                end=end,
                path_nodes=path,
                path_distance=round(self.sdk.path_distance(path), 3),
            )
            self.logger.log_command(ctx, vid, command, task=task, source="v4_replan")
            return command

        command = self.sdk.navigate_to(
            target_zone,
            action={"type": action_type, "target_zone": target_zone},
            from_position=vehicle.get("position"),
            speed=self.CRUISE_SPEED,
        )
        self.logger.log_event(
            "v4_replan",
            ctx,
            vehicle_id=vid,
            other_id=other,
            target_zone=target_zone,
            action_type=action_type,
            fallback=True,
            blocked_nodes=sorted(blocked),
            start=start,
            end=end,
            reason="blocked_path_unavailable",
            path_distance=round(self.sdk.points_distance(command.get("path", [])), 3) if command else None,
        )
        if command:
            self.logger.log_command(ctx, vid, command, task=task, source="v4_replan_fallback")
        return command

    def _build_blocked_nodes(self, vid: str, other: str, ctx: dict,
                             cache: dict[str, dict]) -> set[str]:
        blocked: set[str] = set()
        direction = self._direction_relation(vid, other, cache)
        if direction == "oncoming":
            prev_node = self.memory.last_nodes.get(other)
            if prev_node and prev_node not in {cache[vid]["current_node"], cache[vid].get("next_node")}:
                blocked.add(prev_node)

        next_node = cache.get(other, {}).get("next_node")
        if next_node:
            blocked.add(next_node)

        blocked.update(self._future_nodes(other, ctx, cache, self.BLOCK_LOOKAHEAD))

        return blocked

    def _future_nodes(self, vid: str, ctx: dict, cache: dict[str, dict],
                      limit: int) -> list[str]:
        nodes = []
        current = cache.get(vid, {}).get("current_node")
        for point in ctx["vehicles"].get(vid, {}).get("path_preview", []):
            node = self.sdk.find_nearest_node(point[0], point[1])
            if not node or node == current or (nodes and nodes[-1] == node):
                continue
            nodes.append(node)
            if len(nodes) >= limit:
                break
        return nodes

    def _speed_stagger_vehicle(self, vid: str, other: str, ctx: dict,
                               task: Task) -> Optional[dict]:
        vehicle = ctx["vehicles"].get(vid, {})
        path = vehicle.get("path_preview", [])
        if not path:
            return None
        command = {
            "path": path,
            "action": {"type": task.action_type, "target_zone": task.target_zone},
            "speed": self.CLEARANCE_SPEED,
        }
        self.logger.log_event(
            "v4_replan",
            ctx,
            vehicle_id=vid,
            other_id=other,
            target_zone=task.target_zone,
            action_type=task.action_type,
            mode="speed_stagger",
            fallback=True,
            blocked_nodes=[],
            reason="empty_blocked_nodes",
            path_distance=round(self.sdk.points_distance(path), 3),
        )
        self.logger.log_command(ctx, vid, command, task=task, source="v4_speed_stagger")
        return command

    def _safe_neighbor_node(self, vid: str, ctx: dict,
                            cache: dict[str, dict],
                            reserved_nodes: set[str] | None = None,
                            avoid_nodes: set[str] | None = None) -> Optional[str]:
        current = cache.get(vid, {}).get("current_node")
        if not current:
            return None
        reserved_nodes = reserved_nodes or set()
        avoid_nodes = avoid_nodes or set()
        occupied = {
            data.get("current_node")
            for other, data in cache.items()
            if other != vid
        }
        candidates = []
        for neighbor, _weight in self.sdk._adjacency.get(current, []):
            if neighbor in occupied or neighbor in reserved_nodes or neighbor in avoid_nodes:
                continue
            point = self.sdk.nodes_to_points([neighbor])
            if not point:
                continue
            min_dist = min(
                self._distance(point[0], cache[other]["pos"])
                for other in cache
                if other != vid
            )
            candidates.append((min_dist, neighbor))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    def _cooldown_age(self, vid: str, ctx: dict) -> float:
        return ctx["time"] - self.memory.last_replan_time.get(vid, -1e9)

    def _in_cooldown(self, vid: str, ctx: dict) -> bool:
        return self._cooldown_age(vid, ctx) < self.REPLAN_COOLDOWN_SECONDS

    @staticmethod
    def _pair_key(a: str, b: str) -> tuple[str, str]:
        return tuple(sorted((a, b)))

    def _log_replan_skip(self, event: str, ctx: dict, **payload) -> None:
        pair = payload.get("a"), payload.get("b")
        if payload.get("vehicle_id") and payload.get("other_id"):
            pair = self._pair_key(payload["vehicle_id"], payload["other_id"])
        key = (pair, payload.get("reason"), payload.get("direction"))
        self.logger.log_event_throttled(
            event,
            key=key,
            min_interval=self.V4_EVENT_LOG_INTERVAL,
            state_or_ctx=ctx,
            **payload,
        )

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
        if self._handle_terminal_state(state):
            return {}
        ctx = self._prepare(state)
        self.memory.prune(ctx["vehicles"], ctx["time"], self.STALE_TASK_SECONDS)
        commands = self._compute_commands(ctx)
        commands = self._resolve_time_conflicts(commands)
        self._update_last_nodes(ctx)
        self.logger.log_snapshot(state, self.memory)
        return commands

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
        if self._handle_terminal_state(state):
            return {}
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
                    self.logger.log_command(ctx, vid, commands[vid], source="vn_timeout_release")
                self.memory.active_tasks.pop(vid, None)

        self._update_last_nodes(ctx)
        self.logger.log_snapshot(state, self.memory)
        return commands
