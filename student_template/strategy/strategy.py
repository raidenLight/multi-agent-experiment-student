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


class V0Strategy:
    """V0：纯贪心响应式。每车按固定优先级（送原料→送成品→取成品→取原料），
    选第一个匹配的任务，不评分、不排序、不占用标记。"""

    CRUISE_SPEED = 20.0
    STALE_TASK_SECONDS = 45.0

    def __init__(self, sdk) -> None:
        self.sdk = sdk
        self.memory = StrategyMemory()
        self.logger = RunLogger(self._strategy_version(), sdk)

    def __call__(self, state: dict) -> dict:
        if state.get("type") == "game_over":
            self.logger.final(state.get("score", 0))
            return {}
        self.memory.prune(state.get("vehicles", {}), state.get("time", 0), self.STALE_TASK_SECONDS)
        commands = self._compute_commands(state)
        self.logger.log_snapshot(state, self.memory)
        return commands

    def _compute_commands(self, ctx: dict) -> dict:
        vehicles = ctx.get("vehicles", {})
        zones = ctx.get("zones", {})
        orders = ctx.get("orders", [])
        raw_items = set()
        for z in zones.values():
            if z.get("type") == "raw_material":
                raw_items.update(z.get("outputs", []))

        commands = {}
        for vid in sorted(vehicles, key=vehicle_sort_key):
            v = vehicles[vid]
            if v["status"] != "idle":
                continue
            carrying = v["carrying"]
            pos = v["position"]
            cmd = None

            if carrying and carrying in raw_items:
                for zid in self.sdk.find_zones(input=carrying):
                    zone = zones[zid]
                    if zone["items"].get(carrying, 0) < 1:
                        cmd = self.sdk.navigate_to(zid, action={"type": "drop", "target_zone": zid}, from_position=pos)
                        break
                if not cmd:
                    for zid in self.sdk.find_zones(input=carrying):
                        cmd = self.sdk.navigate_to(zid, action={"type": "drop", "target_zone": zid}, from_position=pos)
                        break
            elif carrying:
                for zid in self.sdk.find_zones(input=carrying):
                    zone = zones[zid]
                    order = zone.get("order")
                    if (zone.get("ready") and order and order.get("status") == "pending" and order.get("required") == carrying):
                        cmd = self.sdk.navigate_to(zid, action={"type": "drop", "target_zone": zid}, from_position=pos)
                        break
            else:
                for order in sorted(orders, key=lambda o: o["deadline"]):
                    product = order["product"]
                    for zid in self.sdk.find_zones(output=product, ready=True):
                        cmd = self.sdk.navigate_to(zid, action={"type": "pick", "target_zone": zid}, from_position=pos)
                        break
                    if cmd:
                        break
                if not cmd:
                    for order in sorted(orders, key=lambda o: o["deadline"]):
                        product = order["product"]
                        for pzid in self.sdk.find_zones(output=product):
                            pz = zones[pzid]
                            for needed_item in pz.get("inputs", []):
                                if pz["items"].get(needed_item, 0) >= 1:
                                    continue
                                for rzid in self.sdk.find_zones(output=needed_item, ready=True):
                                    cmd = self.sdk.navigate_to(rzid, action={"type": "pick", "target_zone": rzid}, from_position=pos)
                                    break
                                if cmd:
                                    break
                            if cmd:
                                break
                        if cmd:
                            break
            if cmd:
                commands[vid] = cmd
                self.logger.log_command(ctx, vid, cmd, source="v0_greedy")

        return commands

    @staticmethod
    def _strategy_version() -> str:
        return "V0"


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

    核心改进（相对 V1 的贪婪首匹配）：
    ─────────────────────────────────────────────────────────
    1. 统一候选打分
       每辆空车评估所有可能的取成品+取原料任务，计算综合评分：
       综合评分 = 订单贡献分 - 距离 * 距离权重
       选评分最高的任务执行，而非 V1 的"先碰到哪个做哪个"。

    2. 取成品的前瞻距离（Look-ahead）
       取成品评分 = 订单分 - 取货距离*4.0 - 后续送货距离*0.7
       避免"取了近处成品但送货很远"的短视行为。
       V1 完全不知道取完货后的送货成本。

    3. 取原料链接订单价值 + 稀缺度
       取原料评分 = 订单分*0.3 + 稀缺度*30.0 - 取货距离*4.0
       稀缺度 = 缺该原料的加工区数 / 可供货的原料区数
       紧缺原料自动获得高分，避免 V1 中原料任务永远竞争不过成品任务。

    4. 送原料选最近缺货加工区
       车辆载原料时遍历所有缺该原料的加工区，选距离最近的，而非
       V1 的固定遍历顺序，避免舍近求远的长距离无效运输。

    评分常量（可通过环境变量微调）：
        PICK_DISTANCE_WEIGHT      = 4.0  (V2_PICK_DIST_WEIGHT)
        DELIVERY_LOOKAHEAD_WEIGHT = 0.7  (V2_DELIV_LOOKAHEAD)
        DROP_DISTANCE_WEIGHT      = 4.0  (V2_DROP_DIST_WEIGHT)
    """

    # ==================================================================
    # 评分常量（可通过环境变量微调）
    # ==================================================================
    PICK_DISTANCE_WEIGHT = float(os.environ.get("V2_PICK_DIST_WEIGHT", "4.0"))
    DELIVERY_LOOKAHEAD_WEIGHT = float(os.environ.get("V2_DELIV_LOOKAHEAD", "0.7"))
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
    """V3：遗憾值驱动的全局排序分配。

    核心改进（相对 V2 的逐车顺序分配）：
    ─────────────────────────────────────────────────────────
    V2（和V1）按车辆ID顺序逐个分配任务——先分配的车先选，可能抢走
    后分配车"唯一适合"的任务。V3 改为两阶段：

    阶段1 — 评分 & 计算遗憾值
       对每辆空闲车，生成所有候选任务列表并计算：
          遗憾值 = (最优得分 - 次优得分) / max(最优任务距离, 1.0)
       - 遗憾值大 = 最优任务远好于备选 → 必须优先保障
       - 遗憾值小 = 多个任务差不多 → 可以等别人先选

    阶段2 — 按遗憾值降序分配
       遗憾值大的车优先选任务。例如车辆A遗憾值=500（唯一适合取B1成品），
       车辆B遗憾值=20（附近有多个原料可取），A先选确保不被B抢占。

    算法本质：贪心 + 全局排序，不是匈牙利全局最优，但相比V2的
    固定ID顺序分配有显著改善（实测+130~175分）。

    评分权重调整：取原料评分中 oscore*0.5（V2为0.3）、scarcity*10.0
    （V2为30.0），增加订单价值权重、减少纯稀缺度权重的占比。
    """

    def _compute_commands(self, ctx: dict) -> dict:
        registry = ClaimRegistry.from_memory(self.memory, ctx["vehicles"])
        self._augment_registry(registry, ctx)
        commands: dict = {}

        # 收集每辆车的最佳候选任务列表
        vehicle_entries = []
        for vid in sorted(ctx["vehicles"], key=vehicle_sort_key):
            vehicle = ctx["vehicles"][vid]
            if vehicle.get("status") != "idle":
                continue
            scored = self._score_all_candidates(vid, vehicle, ctx, registry)
            if len(scored) >= 2:
                best_dist = self._zone_distance(vehicle.get("position"), scored[0][1].target_zone or "")
                regret = (scored[0][0] - scored[1][0]) / max(best_dist, 1.0)
            elif len(scored) == 1:
                best_dist = self._zone_distance(vehicle.get("position"), scored[0][1].target_zone or "")
                regret = scored[0][0] / max(best_dist, 1.0)
            else:
                continue
            vehicle_entries.append((regret, vid, vehicle, scored))

        # 按遗憾值降序排列：遗憾大的先分配
        vehicle_entries.sort(key=lambda x: x[0], reverse=True)

        for regret, vid, vehicle, scored in vehicle_entries:
            task = self._choose_task(vid, vehicle, ctx, registry)
            if not task:
                continue
            cmd = self._build_command(vehicle, task, ctx, vehicle_id=vid, registry=registry)
            if not cmd:
                continue
            registry.claim(task)
            self.memory.active_tasks[vid] = ActiveTask(
                vehicle_id=vid, task=task, assigned_at=ctx["time"],
                start_carrying=vehicle.get("carrying"))
            commands[vid] = cmd
            self.logger.log_command(ctx, vid, cmd, task=task)

        return commands

    def _score_all_candidates(self, vehicle_id, vehicle, ctx, registry) -> list:
        """返回该车所有候选任务的 (score, task) 列表，按得分降序。"""
        carrying = vehicle.get("carrying")
        pos = vehicle.get("position")
        candidates = []

        if carrying and carrying in ctx["raw_items"]:
            for zid in ctx["proc_zones"]:
                z = ctx["zones"][zid]
                if carrying not in z.get("inputs", []):
                    continue
                if z.get("items", {}).get(carrying, 0) + registry.material_in_transit(zid, carrying) >= 1:
                    continue
                task = Task(kind=TaskKind.DROP_MATERIAL, item=carrying, drop_zone=zid,
                            priority=20.0, reason=f"送 {carrying} 到 {zid}")
                if registry.can_claim(task):
                    dist = self._zone_distance(pos, zid)
                    candidates.append((20.0 - dist * V2Strategy.PICK_DISTANCE_WEIGHT, task))
        elif carrying:
            for o in ctx["pending_orders"]:
                if order_product(o) != carrying:
                    continue
                c = order_consumer(o)
                if not c or not ctx["zones"].get(c, {}).get("ready"):
                    continue
                base = self._order_priority(ctx, o)
                task = Task(kind=TaskKind.DROP_PRODUCT, item=carrying, drop_zone=c,
                            order_id=order_id(o), priority=base,
                            reason=f"送 {carrying} 到 {c}")
                if registry.can_claim(task):
                    dist = self._zone_distance(pos, c)
                    candidates.append((base - dist * V2Strategy.PICK_DISTANCE_WEIGHT, task))
        else:
            for o in self._sorted_orders(ctx):
                p = order_product(o)
                oscore = self._order_priority(ctx, o)
                consumer = order_consumer(o)
                for zid in ctx["proc_zones"]:
                    z = ctx["zones"][zid]
                    if p not in z.get("outputs", []) or not z.get("ready"):
                        continue
                    task = Task(kind=TaskKind.PICK_PRODUCT, item=p, pick_zone=zid,
                                order_id=order_id(o), priority=oscore,
                                reason=f"取成品 {p}")
                    if registry.can_claim(task):
                        pick_d = self._zone_distance(pos, zid)
                        deliv_d = self._lookahead_distance(zid, consumer)
                        score = (oscore - pick_d * self.PICK_DISTANCE_WEIGHT
                                 - deliv_d * self.DELIVERY_LOOKAHEAD_WEIGHT)
                        candidates.append((score, task))
            for o in self._sorted_orders(ctx):
                p = order_product(o)
                oscore = self._order_priority(ctx, o)
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
                            task = Task(kind=TaskKind.PICK_RAW, item=item,
                                        pick_zone=rzid, priority=10.0,
                                        reason=f"取原料 {item}")
                            if registry.can_claim(task):
                                dist = self._zone_distance(pos, rzid)
                                scarcity = self._material_scarcity(item, ctx)
                                score = (oscore * 0.5 + scarcity * 10.0
                                         - dist * self.PICK_DISTANCE_WEIGHT)
                                candidates.append((score, task))
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates


class V3_1Strategy(V3Strategy):
    """V3_1：V3 + 智能放弃（Smart Abandon）。

    核心改进（相对 V3）：中途取消低价值的长途运输任务
    ─────────────────────────────────────────────────────────
    问题场景：车辆载着原料 A2 正赶往 150m 外的加工区 proc_b5，
    途中经过加工区 proc_b1 发现成品 B1（价值 15000 的订单）已完成待取。
    V3 会让车辆继续送完 150m 的原料才能接新任务。

    V3_1 的智能放弃：
       条件1：车辆正在运输原料（carrying in raw_items）
       条件2：剩余距离 >= 40米（原料运送还很长，功亏一篑不划算）
       条件3：附近有成品可取，距离 <= 30米（替代任务近在咫尺）
       → 满足全部三个条件 → 发送 abandon 命令丢弃原料

    设计考量：
       - 严格的双阈值条件（40m+30m）防止滥用，避免频繁切换导致震荡
       - 只放弃原料运输（drop_reward=10），换成取成品（order_value~9000+）
       - 不放弃正在送的成品——成品价值太高，丢弃损失巨大
       - 不放弃快送达的原料（<40m）——放弃的沉没成本大于收益

    实测效果：+168 vs V3（碰撞从 1540 降至 1392，因减少了无效长途行程）
    """

    def _compute_commands(self, ctx: dict) -> dict:
        commands = V3Strategy._compute_commands(self, ctx)
        registry = ClaimRegistry.from_memory(self.memory, ctx["vehicles"])
        self._augment_registry(registry, ctx)

        for vid, active in list(self.memory.active_tasks.items()):
            if vid in commands: continue
            vehicle = ctx["vehicles"].get(vid)
            if not vehicle or vehicle.get("status") != "moving": continue
            if vehicle.get("carrying") not in ctx.get("raw_items", set()): continue
            # 只考虑载原料的长途运输
            cur_dist = self._zone_distance(vehicle.get("position"), active.task.target_zone or "")
            if cur_dist < 40.0: continue

            # 检查是否有近距离成品可取
            pos = vehicle.get("position")
            for o in self._sorted_orders(ctx):
                p = order_product(o)
                for zid in ctx["proc_zones"]:
                    z = ctx["zones"][zid]
                    if p not in z.get("outputs", []) or not z.get("ready"): continue
                    pick_d = self._zone_distance(pos, zid)
                    if pick_d > 30.0: continue  # 成品太远不放弃
                    # 成品近+原料运距远→放弃
                    commands[vid] = {"path": [], "action": {"type": "abandon"}}
                    self.memory.active_tasks.pop(vid, None)
                    self.logger.log_command(ctx, vid, commands[vid], task=active.task, source="v31_abandon")
                    break
                if vid in commands: break
        return commands


class V3_2Strategy(V3_1Strategy):
    """V3_2: V3_1 + 动态路径时间线协调。

    在V3_1分配任务后：
    1. 收集所有车辆的规划路径，构建边级占用图
    2. 仅对高占用边(≥3车) + 多冲突边(≥2条)的车辆尝试绕行
    3. 绕行比 ≤ 1.08 才接受，惩罚与占用数成正比
    4. 无冲突或冲突轻微时完全不影响（保持V3_1原路径）
    """

    CONFLICT_PENALTY_BASE: float = 12.0   # 冲突边基础惩罚/每车
    MAX_DETOUR_RATIO: float = 1.15         # 最大绕行比
    MIN_CONFLICT_EDGES: int = 1            # 至少1条冲突边就触发
    MIN_OCCUPANCY_TO_PENALIZE: int = 2     # ≥2辆车就惩罚

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def _compute_commands(self, ctx: dict) -> dict:
        """V3_1逻辑 + 路径时间线协调。"""
        # 第一阶段：V3_1的完整逻辑（任务分配 + 智能放弃）
        commands = V3_1Strategy._compute_commands(self, ctx)

        # 第二阶段：构建全车队的边占用图
        edge_occ = self._build_fleet_edge_occupancy(ctx, commands)

        if not edge_occ:
            return commands

        # 第三阶段：对冲突车辆尝试绕行
        registry = ClaimRegistry.from_memory(self.memory, ctx["vehicles"])
        self._augment_registry(registry, ctx)

        for vid, cmd in list(commands.items()):
            if not cmd.get("path"):
                continue
            vehicle = ctx["vehicles"].get(vid)
            active = self.memory.active_tasks.get(vid)
            if not vehicle or not active:
                continue

            old_nodes = self._points_to_nodes(cmd["path"])
            if len(old_nodes) < 2:
                continue

            # 检查该车路径上的冲突边（需达到最小冲突数）
            conflicted_edges = []
            for i in range(len(old_nodes) - 1):
                e = (old_nodes[i], old_nodes[i+1])
                if edge_occ.get(e, 0) > 1:  # >1 = 有其他车也要用
                    conflicted_edges.append(e)

            if len(conflicted_edges) < self.MIN_CONFLICT_EDGES:
                continue  # 冲突太少不值得绕行

            # 尝试绕行：先从占用图中移除本车贡献
            for i in range(len(old_nodes) - 1):
                e = (old_nodes[i], old_nodes[i+1])
                edge_occ[e] = max(0, edge_occ.get(e, 0) - 1)

            # 构建分级冲突惩罚：仅惩罚高占用边（≥3辆车）
            penalties = {}
            for e, cnt in edge_occ.items():
                if cnt >= self.MIN_OCCUPANCY_TO_PENALIZE:
                    penalties[e] = cnt * self.CONFLICT_PENALTY_BASE

            start_pos = vehicle.get("position")
            start_node = self.sdk.find_nearest_node(start_pos[0], start_pos[1])
            end_node = self.sdk.get_zone_node(active.task.target_zone or "")

            if not start_node or not end_node:
                # 恢复占用
                for i in range(len(old_nodes) - 1):
                    e = (old_nodes[i], old_nodes[i+1])
                    edge_occ[e] = edge_occ.get(e, 0) + 1
                continue

            new_path = self.sdk.plan_path_with_edge_penalty(
                start_node, end_node, penalties)
            if new_path and len(new_path) >= 2 and new_path[-1] == end_node:
                # 验证绕行是否真正躲避了冲突边
                new_conflicted = 0
                for i in range(len(new_path) - 1):
                    e = (new_path[i], new_path[i+1])
                    if e in conflicted_edges:
                        new_conflicted += 1

                # 必须减少了冲突边，且绕行比可接受
                if new_conflicted < len(conflicted_edges):
                    shortest = self.sdk.plan_path(start_node, end_node)
                    if shortest:
                        ratio = (self.sdk.path_distance(new_path) /
                                 max(self.sdk.path_distance(shortest), 1.0))
                        if ratio <= self.MAX_DETOUR_RATIO:
                            # 接受绕行路径
                            commands[vid] = {
                                "path": self.sdk.nodes_to_points(new_path),
                                "action": cmd["action"],
                                "speed": self.CRUISE_SPEED,
                            }
                            # 更新占用图
                            for i in range(len(new_path) - 1):
                                e = (new_path[i], new_path[i+1])
                                edge_occ[e] = edge_occ.get(e, 0) + 1
                            continue

            # 无法绕行：恢复原路径占用
            for i in range(len(old_nodes) - 1):
                e = (old_nodes[i], old_nodes[i+1])
                edge_occ[e] = edge_occ.get(e, 0) + 1

        return commands

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _build_fleet_edge_occupancy(self, ctx, commands) -> dict:
        """构建全车队的边占用图 {(from, to): count}。

        包含：
        - 新分配车辆（来自commands）
        - 已在途车辆（来自active_tasks但不在commands中）
        """
        edge_occ: dict = {}

        # 新分配车辆的路径
        for vid, cmd in commands.items():
            if not cmd.get("path"):
                continue
            nodes = self._points_to_nodes(cmd["path"])
            for i in range(len(nodes) - 1):
                e = (nodes[i], nodes[i+1])
                edge_occ[e] = edge_occ.get(e, 0) + 1

        # 已在途车辆：估算其剩余路径
        for vid, active in self.memory.active_tasks.items():
            if vid in commands:
                continue  # 已在上一步统计
            vehicle = ctx["vehicles"].get(vid)
            if not vehicle or vehicle.get("status") != "moving":
                continue
            if not active.task or not active.task.target_zone:
                continue

            start_pos = vehicle.get("position")
            start_node = self.sdk.find_nearest_node(start_pos[0], start_pos[1])
            end_node = self.sdk.get_zone_node(active.task.target_zone)
            if not start_node or not end_node:
                continue

            path = self.sdk.plan_path(start_node, end_node)
            if path and len(path) >= 2:
                for i in range(len(path) - 1):
                    e = (path[i], path[i+1])
                    edge_occ[e] = edge_occ.get(e, 0) + 1

        return edge_occ

    def _points_to_nodes(self, points: list) -> list:
        """坐标路径 → 节点路径（连续去重）。"""
        nodes = []
        for pt in points:
            n = self.sdk.find_nearest_node(pt[0], pt[1])
            if n and (not nodes or n != nodes[-1]):
                nodes.append(n)
        return nodes


class V4Strategy(V3_1Strategy):
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
