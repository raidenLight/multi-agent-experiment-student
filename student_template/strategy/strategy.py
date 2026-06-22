"""策略实现：单一类继承树，每个版本只重写自己改动的部分。

V1 → V2 → V3 → V4 → V5 → VN
"""

from __future__ import annotations

import os
import random
from typing import Optional

from .logger import RunLogger
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

    def _zone_distance(self, pos, zone_id: str) -> float:
        if not self.sdk or not pos:
            return float("inf")
        return self.sdk.zone_distance(pos, zone_id)

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
    """V2：综合订单收益、紧急度、路径距离、原料紧缺度进行智能调度。"""

    # ==================================================================
    # 评分组件
    # ==================================================================

    def _order_priority(self, ctx: dict, order: dict) -> float:
        """订单综合评分 = 收益 + 紧急度。"""
        deadline = float(order.get("deadline", float("inf")))
        urgency = urgency_score(ctx["time"], deadline)
        value = self._product_value(order_product(order))
        return value * 1.0 + urgency * 80.0

    def _product_value(self, product: str) -> float:
        if not self.sdk or not self.sdk.recipes:
            return 0.0
        return float(self.sdk.recipes.get(product, {}).get("value", 0))

    def _material_scarcity(self, item: str, ctx: dict) -> float:
        """原料紧缺度 = 缺该原料的加工区数 ÷ 可取的原料区数。越高越值得取。"""
        need = sum(1 for zid in ctx["proc_zones"]
                   if item in ctx["zones"][zid].get("inputs", [])
                   and ctx["zones"][zid].get("items", {}).get(item, 0) == 0)
        have = sum(1 for zid in ctx["raw_zones"]
                   if item in ctx["zones"][zid].get("outputs", [])
                   and ctx["zones"][zid].get("ready"))
        return need / max(have, 1)

    def _sorted_orders(self, ctx: dict) -> list[dict]:
        return sorted(ctx["pending_orders"],
                      key=lambda o: self._order_priority(ctx, o), reverse=True)

    # ==================================================================
    # 重写任务选择 — 全部加入距离排序 + 评分
    # ==================================================================

    def _choose_material_drop(self, pos: list, item: str, ctx: dict,
                              registry: ClaimRegistry) -> Optional[Task]:
        """送原料到最近且缺货的加工区。"""
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
                candidates.append((self._zone_distance(pos, zid), task))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
        return None

    def _choose_product_drop(self, pos: list, item: str, ctx: dict,
                             registry: ClaimRegistry) -> Optional[Task]:
        """送成品到综合评分最高的订单消费区。"""
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
        """空车：取成品和取原料候选放入同一池，统一评分选最优。"""
        candidates = []

        # ---- 候选：取成品 ----
        for order in self._sorted_orders(ctx):
            product = order_product(order)
            order_score = self._order_priority(ctx, order)
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
                    dist = self._zone_distance(pos, zid)
                    # 取成品直接得分 = 订单价值（直接收益）
                    score = order_score - dist * 0.1
                    candidates.append((score, task))

        # ---- 候选：取原料 ----
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
                            # 取原料间接得分 = 对订单的贡献 + 紧缺度
                            score = (order_score * 0.5
                                     + scarcity * 10.0
                                     - dist * 0.1)
                            candidates.append((score, task))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
        return None


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
    """V4：合法锚点避让 + 低扰动持续冲突处理。"""

    COLLISION_WARN_DISTANCE = 3.5
    IDLE_CLEAR_DISTANCE = 2.0
    CLEARANCE_SPEED = 16.0
    REPLAN_COOLDOWN_SECONDS = 1.0
    CLEARANCE_COOLDOWN_SECONDS = 2.5
    EXPECTED_GAIN_WEIGHT = 0.03
    RANDOM_REPLAN_PROB = 0.15
    BLOCK_LOOKAHEAD = 3
    SUSTAINED_CONFLICT_SECONDS = 1.2
    SUSTAINED_CONFLICT_TICKS = 2
    DETOUR_RATIO_LIMIT = 1.25
    SAME_DIRECTION_SPEED = 8.0
    TARGET_STAGING_DISTANCE = 2.0
    V4_EVENT_LOG_INTERVAL = 1.0
    DEFAULT_RAW_REWARD = 30.0
    DEFAULT_PRODUCT_REWARD = 100.0
    CONGESTION_CURRENT_PENALTY = 60.0
    CONGESTION_NEXT_PENALTY = 35.0
    CONGESTION_FUTURE_PENALTY = 18.0
    CONGESTION_TARGET_PENALTY = 25.0
    EMERGENCY_KEEPER_STATUS_BONUS = 8.0
    PREDISPATCH_SPEED = 18.0
    PREDISPATCH_HOLD_SECONDS = 8.0
    PREDISPATCH_MIN_DISTANCE = 6.0

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
        self.avoidance_enabled = (
            os.environ.get("V4_AVOIDANCE", "1").lower()
            in {"1", "true", "yes", "on"}
        )
        self.moving_replan_enabled = (
            os.environ.get("V4_MOVING_REPLAN", "0").lower()
            in {"1", "true", "yes", "on"}
        )
        self.target_staging_enabled = (
            os.environ.get("V4_TARGET_STAGING", "0").lower()
            in {"1", "true", "yes", "on"}
        )
        self.predispatch_enabled = (
            os.environ.get("V4_PREDISPATCH", "0").lower()
            in {"1", "true", "yes", "on"}
        )
        self.parallel_raw_pick_enabled = (
            os.environ.get("V4_PARALLEL_RAW_PICK", "0").lower()
            in {"1", "true", "yes", "on"}
        )
        self.abandon_stale_raw_enabled = (
            os.environ.get("V4_ABANDON_STALE_RAW", "0").lower()
            in {"1", "true", "yes", "on"}
        )
        self.same_direction_stagger_enabled = (
            os.environ.get("V4_SAME_DIRECTION_STAGGER", "0").lower()
            in {"1", "true", "yes", "on"}
        )
        self.CLEARANCE_SPEED = self._env_float("V4_CLEARANCE_SPEED", self.CLEARANCE_SPEED)
        self.PREDISPATCH_SPEED = self._env_float("V4_PREDISPATCH_SPEED", self.PREDISPATCH_SPEED)
        self.REPLAN_COOLDOWN_SECONDS = self._env_float(
            "V4_REPLAN_COOLDOWN", self.REPLAN_COOLDOWN_SECONDS
        )
        self.COLLISION_WARN_DISTANCE = self._env_float(
            "V4_WARN_DISTANCE", self.COLLISION_WARN_DISTANCE
        )
        self.DETOUR_RATIO_LIMIT = self._env_float(
            "V4_DETOUR_RATIO", self.DETOUR_RATIO_LIMIT
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
        self.memory.reserved_staging_nodes.clear()
        self.memory.reserved_predispatch_nodes.clear()
        commands = self._compute_commands(ctx)
        if self.avoidance_enabled:
            commands.update(self._build_replan_overrides(ctx, commands))
        self._update_last_nodes(ctx)
        self.logger.log_snapshot(state, self.memory)
        return commands

    def _compute_commands(self, ctx: dict) -> dict:
        """V4 adds no-action pre-dispatch after normal task assignment fails."""
        registry = ClaimRegistry.from_memory(self.memory, ctx["vehicles"])
        self._augment_registry(registry, ctx)
        commands = {}

        for vid in sorted(ctx["vehicles"], key=vehicle_sort_key):
            vehicle = ctx["vehicles"][vid]
            if vehicle.get("status") != "idle":
                continue

            task = self._choose_task(vid, vehicle, ctx, registry)
            if task:
                command = self._build_command(
                    vehicle, task, ctx, vehicle_id=vid, registry=registry
                )
                if command:
                    registry.claim(task)
                    self.memory.active_tasks[vid] = ActiveTask(
                        vehicle_id=vid, task=task,
                        assigned_at=ctx["time"],
                        start_carrying=vehicle.get("carrying"),
                    )
                    commands[vid] = command
                    self.logger.log_command(ctx, vid, command, task=task)
                    continue

            command = self._build_predispatch_command(vid, vehicle, ctx, registry)
            if command:
                commands[vid] = command

        return commands

    def _choose_empty_vehicle_task(self, pos: list, ctx: dict,
                                   registry: ClaimRegistry) -> Optional[Task]:
        task = super()._choose_empty_vehicle_task(pos, ctx, registry)
        if task or not self.parallel_raw_pick_enabled:
            return task
        return self._choose_parallel_raw_pick_task(pos, ctx, registry)

    def _choose_task(self, vehicle_id: str, vehicle: dict,
                     ctx: dict, registry: ClaimRegistry) -> Optional[Task]:
        task = super()._choose_task(vehicle_id, vehicle, ctx, registry)
        if task or not self.abandon_stale_raw_enabled:
            return task

        carrying = vehicle.get("carrying")
        if carrying in ctx.get("raw_items", set()) and self._raw_item_has_no_pending_use(carrying, ctx):
            return Task(
                kind=TaskKind.ABANDON,
                item=carrying,
                priority=-1.0,
                reason=f"丢弃当前订单不需要的原料 {carrying}",
            )
        return None

    def _raw_item_has_no_pending_use(self, item: str, ctx: dict) -> bool:
        pending_products = {order_product(order) for order in ctx.get("pending_orders", [])}
        if not pending_products:
            return True
        for zid in ctx["proc_zones"]:
            zone = ctx["zones"][zid]
            product = (zone.get("outputs") or [None])[0]
            if product in pending_products and item in zone.get("inputs", []):
                return False
        return True

    def _choose_parallel_raw_pick_task(self, pos: list, ctx: dict,
                                       registry: ClaimRegistry) -> Optional[Task]:
        """Use the second raw-material stock when multiple downstream slots need it.

        V3 treats a raw zone as single-claim even though raw zones can hold two
        items. This keeps some empty vehicles idle while one vehicle heads to a
        source with enough inventory for another useful pickup.
        """
        pressures = self._pending_product_pressures(ctx)
        if not pressures:
            return None

        candidates = []
        for rzid in ctx["raw_zones"]:
            raw_zone = ctx["zones"][rzid]
            outputs = raw_zone.get("outputs", [])
            if not outputs:
                continue
            item = outputs[0]
            stock = raw_zone.get("items", {}).get(item, 0)
            claimed = registry.raw_pick_count(rzid)
            if not raw_zone.get("ready") or stock <= claimed:
                continue

            missing_pressure = 0.0
            missing_slots = 0
            for pzid in ctx["proc_zones"]:
                proc_zone = ctx["zones"][pzid]
                if item not in proc_zone.get("inputs", []):
                    continue
                product = (proc_zone.get("outputs") or [None])[0]
                pressure = pressures.get(product or "", 0.0)
                if pressure <= 0:
                    continue
                if proc_zone.get("items", {}).get(item, 0) + registry.material_in_transit(pzid, item) >= 1:
                    continue
                missing_slots += 1
                missing_pressure += pressure

            if missing_slots <= claimed:
                continue

            dist = self._zone_distance(pos, rzid)
            scarcity = self._material_scarcity(item, ctx)
            score = missing_pressure * 0.35 + scarcity * 12.0 - dist * 0.08
            task = Task(
                kind=TaskKind.PICK_RAW,
                item=item,
                pick_zone=rzid,
                priority=score,
                reason=f"并行预取原料 {item}",
            )
            candidates.append((score, -dist, rzid, task))

        if not candidates:
            return None
        candidates.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        return candidates[0][3]

    def _build_command(self, vehicle: dict, task: Task,
                       ctx: dict = None, vehicle_id: str = None,
                       registry: ClaimRegistry = None) -> Optional[dict]:
        if task.kind in {TaskKind.WAIT, TaskKind.ABANDON}:
            return super()._build_command(vehicle, task, ctx, vehicle_id, registry)

        target_zone = task.target_zone
        action_type = task.action_type
        if not target_zone or not action_type:
            return None
        if not self.sdk or not ctx:
            return super()._build_command(vehicle, task, ctx, vehicle_id, registry)

        if self.avoidance_enabled and self.target_staging_enabled:
            staging = self._target_staging_command(vehicle, task, ctx, vehicle_id)
            if staging:
                return staging

        if not self.congestion_path_enabled:
            return super()._build_command(vehicle, task, ctx, vehicle_id, registry)

        start, _prefix = self._legal_anchor(vehicle)
        end = self.sdk.get_zone_node(target_zone)
        if not start or not end:
            return super()._build_command(vehicle, task, ctx, vehicle_id, registry)

        penalties = self._congestion_penalties(ctx, vehicle_id, end)
        path = self.sdk.plan_path_with_penalty(start, end, penalties)
        if not path:
            return super()._build_command(vehicle, task, ctx, vehicle_id, registry)

        command = self._command_from_node_path(
            vehicle,
            path,
            {"type": action_type, "target_zone": target_zone},
            self.CRUISE_SPEED,
        )
        if not command:
            return super()._build_command(vehicle, task, ctx, vehicle_id, registry)
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

    def _build_predispatch_command(self, vehicle_id: str, vehicle: dict,
                                   ctx: dict, registry: ClaimRegistry) -> Optional[dict]:
        if not self.predispatch_enabled or not self.sdk:
            return None

        start, _prefix = self._legal_anchor(vehicle)
        if not start:
            return None

        current_node = self._node_from_position(vehicle.get("position"))
        last_node = self.memory.last_predispatch_node.get(vehicle_id)
        last_time = self.memory.last_predispatch_time.get(vehicle_id, -1e9)
        if (current_node and current_node == last_node
                and ctx["time"] - last_time < self.PREDISPATCH_HOLD_SECONDS):
            return None

        candidates = self._predispatch_candidates(vehicle_id, vehicle, ctx, registry)
        for score, target_node, target_zone, mode, reason in candidates:
            if start == target_node:
                self.memory.last_predispatch_time[vehicle_id] = ctx["time"]
                self.memory.last_predispatch_node[vehicle_id] = target_node
                continue

            node_path = self.sdk.plan_path(start, target_node)
            if not node_path or len(node_path) < 2:
                continue
            distance = self.sdk.path_distance(node_path)
            if distance < self.PREDISPATCH_MIN_DISTANCE:
                continue

            command = self._command_from_node_path(
                vehicle, node_path, None, self.PREDISPATCH_SPEED
            )
            if not command:
                continue

            self.memory.reserved_predispatch_nodes.add(target_node)
            self.memory.last_predispatch_time[vehicle_id] = ctx["time"]
            self.memory.last_predispatch_node[vehicle_id] = target_node
            self.logger.log_event_throttled(
                "v4_predispatch",
                key=(vehicle_id, target_node, mode, reason),
                min_interval=self.V4_EVENT_LOG_INTERVAL,
                state_or_ctx=ctx,
                vehicle_id=vehicle_id,
                mode=mode,
                target_zone=target_zone,
                target_node=target_node,
                carrying=vehicle.get("carrying"),
                score=round(score, 3),
                reason=reason,
                path_nodes=node_path,
                path_distance=round(distance, 3),
            )
            self.logger.log_command(ctx, vehicle_id, command, source="v4_predispatch")
            return command

        return None

    def _predispatch_candidates(self, vehicle_id: str, vehicle: dict,
                                ctx: dict, registry: ClaimRegistry
                                ) -> list[tuple[float, str, str, str, str]]:
        pos = vehicle.get("position")
        if not pos:
            return []

        pressures = self._pending_product_pressures(ctx)
        if not pressures:
            return []

        carrying = vehicle.get("carrying")
        if carrying:
            # Carrying vehicles wait for a real drop assignment. Experiments
            # showed that staging them near processing entrances creates
            # congestion and starves the order pipeline.
            return []

        candidates = []
        candidates.extend(self._empty_product_predispatch_candidates(
            vehicle_id, pos, ctx, pressures
        ))
        candidates.extend(self._empty_raw_predispatch_candidates(
            vehicle_id, pos, ctx, registry, pressures
        ))
        candidates.sort(key=lambda x: (-x[0], x[1], x[2], x[3]))
        return candidates

    def _raw_carry_predispatch_candidates(
            self, vehicle_id: str, item: str, pos: list, ctx: dict,
            registry: ClaimRegistry, pressures: dict[str, float]
            ) -> list[tuple[float, str, str, str, str]]:
        candidates = []
        for zid in ctx["proc_zones"]:
            zone = ctx["zones"][zid]
            if item not in zone.get("inputs", []):
                continue
            product = (zone.get("outputs") or [None])[0]
            pressure = pressures.get(product or "", 0.0)
            if pressure <= 0:
                continue

            target_node = self.sdk.get_zone_node(zid)
            staging = self._predispatch_staging_node(target_node, ctx, vehicle_id)
            if not staging:
                continue

            current = zone.get("items", {}).get(item, 0)
            in_transit = registry.material_in_transit(zid, item)
            slot_bonus = 55.0 if current + in_transit < 1 else 15.0
            product_value = self._product_value(product or "")
            distance = self._zone_distance(pos, zid)
            score = (
                120.0 + slot_bonus + product_value * 0.35
                + pressure * 0.12 - distance * 0.08
            )
            reason = "carry_raw_slot" if current + in_transit < 1 else "carry_raw_future"
            candidates.append((score, staging, zid, "carry_raw", reason))

        candidates.sort(key=lambda x: (-x[0], x[1], x[2], x[3]))
        return candidates

    def _product_carry_predispatch_candidates(
            self, vehicle_id: str, product: str, pos: list, ctx: dict,
            pressures: dict[str, float]) -> list[tuple[float, str, str, str, str]]:
        candidates = []
        for order in self._sorted_orders(ctx):
            if order_product(order) != product:
                continue
            consumer = order_consumer(order)
            if not consumer:
                continue
            target_node = self.sdk.get_zone_node(consumer)
            staging = self._predispatch_staging_node(target_node, ctx, vehicle_id)
            if not staging:
                continue
            distance = self._zone_distance(pos, consumer)
            score = 180.0 + pressures.get(product, 0.0) * 0.2 - distance * 0.1
            candidates.append((score, staging, consumer, "carry_product", "pending_order"))
        candidates.sort(key=lambda x: (-x[0], x[1], x[2], x[3]))
        return candidates

    def _empty_product_predispatch_candidates(
            self, vehicle_id: str, pos: list, ctx: dict,
            pressures: dict[str, float]) -> list[tuple[float, str, str, str, str]]:
        candidates = []
        for zid in ctx["proc_zones"]:
            zone = ctx["zones"][zid]
            product = (zone.get("outputs") or [None])[0]
            pressure = pressures.get(product or "", 0.0)
            if pressure <= 0:
                continue
            progress = float(zone.get("progress") or 0.0)
            if not zone.get("ready") and progress <= 0:
                continue

            target_node = self.sdk.get_zone_node(zid)
            staging = self._predispatch_staging_node(target_node, ctx, vehicle_id)
            if not staging:
                continue

            ready_bonus = 45.0 if zone.get("ready") else 0.0
            distance = self._zone_distance(pos, zid)
            score = (
                55.0 + ready_bonus + self._product_value(product or "") * 0.25
                + pressure * 0.1 + progress * 0.2 - distance * 0.08
            )
            reason = "product_ready_claimed" if zone.get("ready") else "product_soon"
            candidates.append((score, staging, zid, "empty_product", reason))

        candidates.sort(key=lambda x: (-x[0], x[1], x[2], x[3]))
        return candidates

    def _empty_raw_predispatch_candidates(
            self, vehicle_id: str, pos: list, ctx: dict,
            registry: ClaimRegistry, pressures: dict[str, float]
            ) -> list[tuple[float, str, str, str, str]]:
        candidates = []
        needed_items: dict[str, float] = {}
        for zid in ctx["proc_zones"]:
            zone = ctx["zones"][zid]
            product = (zone.get("outputs") or [None])[0]
            pressure = pressures.get(product or "", 0.0)
            if pressure <= 0:
                continue
            for item in zone.get("inputs", []):
                if zone.get("items", {}).get(item, 0) + registry.material_in_transit(zid, item) >= 1:
                    continue
                needed_items[item] = max(needed_items.get(item, 0.0), pressure)

        for rzid in ctx["raw_zones"]:
            zone = ctx["zones"][rzid]
            outputs = zone.get("outputs", [])
            if not outputs:
                continue
            item = outputs[0]
            pressure = needed_items.get(item, 0.0)
            if pressure <= 0:
                continue
            if registry.raw_pick_zones and rzid in registry.raw_pick_zones and not zone.get("ready"):
                # Another vehicle is already heading to the same not-yet-ready source.
                continue

            target_node = self.sdk.get_zone_node(rzid)
            staging = self._predispatch_staging_node(target_node, ctx, vehicle_id)
            if not staging:
                continue
            stock = sum(zone.get("items", {}).values())
            ready_bonus = 25.0 if zone.get("ready") or stock > 0 else 0.0
            progress = float(zone.get("progress") or 0.0)
            distance = self._zone_distance(pos, rzid)
            score = (
                35.0 + ready_bonus + pressure * 0.12
                + self._material_scarcity(item, ctx) * 8.0
                + progress * 0.1 - distance * 0.06
            )
            reason = "raw_ready_claimed" if zone.get("ready") else "raw_soon"
            candidates.append((score, staging, rzid, "empty_raw", reason))

        candidates.sort(key=lambda x: (-x[0], x[1], x[2], x[3]))
        return candidates

    def _pending_product_pressures(self, ctx: dict) -> dict[str, float]:
        pressures: dict[str, float] = {}
        for order in ctx.get("pending_orders", []):
            product = order_product(order)
            if not product:
                continue
            pressures[product] = pressures.get(product, 0.0) + self._order_priority(ctx, order)
        return pressures

    def _predispatch_staging_node(self, target_node: str | None, ctx: dict,
                                  vehicle_id: str | None) -> Optional[str]:
        if not target_node:
            return None
        staging = self._staging_node_for_target(target_node, ctx, vehicle_id)
        if staging:
            return staging

        occupied = self._predispatch_occupied_nodes(ctx, vehicle_id)
        if target_node not in occupied:
            return target_node
        return None

    def _predispatch_occupied_nodes(self, ctx: dict, vehicle_id: str | None) -> set[str]:
        occupied = set(self.memory.reserved_clearance_nodes)
        occupied.update(self.memory.reserved_staging_nodes)
        occupied.update(self.memory.reserved_predispatch_nodes)
        for other, vehicle in ctx.get("vehicles", {}).items():
            if other == vehicle_id:
                continue
            node = self._node_from_position(vehicle.get("position"))
            if node:
                occupied.add(node)
            for point in (vehicle.get("path_preview") or [])[:1]:
                next_node = self._node_from_position(point)
                if next_node:
                    occupied.add(next_node)
        return occupied

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
                pair_memory = self._update_conflict_memory(a, b, ctx, cache)
                sustained = self._is_sustained_conflict(pair_memory)
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
                    conflict_count=pair_memory["count"],
                    conflict_age=round(pair_memory["age"], 3),
                    sustained=sustained,
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
                            self.memory.last_clearance_time[clearance_vid] = ctx["time"]
                            continue

                if not sustained:
                    self._log_replan_skip(
                        "v4_replan_skip",
                        ctx,
                        a=a,
                        b=b,
                        reason="not_sustained_conflict",
                        direction=direction,
                    )
                    continue

                if direction == "same_direction" and self.same_direction_stagger_enabled:
                    follower = self._choose_same_direction_follower(a, b, ctx, expected_gains, cache)
                    if follower and follower not in replanned:
                        active = self.memory.active_tasks.get(follower)
                        if not active:
                            cmd = self._speed_stagger_no_task_vehicle(follower, b if follower == a else a, ctx)
                            if cmd:
                                overrides[follower] = cmd
                                replanned.add(follower)
                                self.memory.last_replan_time[follower] = ctx["time"]
                                continue

                if not self.moving_replan_enabled:
                    self._log_replan_skip(
                        "v4_replan_skip",
                        ctx,
                        a=a,
                        b=b,
                        reason="moving_replan_disabled",
                        direction=direction,
                    )
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
                if ctx["vehicles"].get(vid, {}).get("status") != "idle":
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

    def _update_conflict_memory(self, a: str, b: str, ctx: dict,
                                cache: dict[str, dict]) -> dict[str, float | int]:
        pair = self._pair_key(a, b)
        now = ctx["time"]
        last_seen = self.memory.close_pair_last_seen.get(pair, -1e9)
        if now - last_seen > self.SUSTAINED_CONFLICT_SECONDS:
            count = 1
            first_seen = now
        else:
            count = self.memory.close_pair_counts.get(pair, 0) + 1
            first_seen = self.memory.close_pair_first_seen.get(pair, now)
        self.memory.close_pair_counts[pair] = count
        self.memory.close_pair_first_seen[pair] = first_seen
        self.memory.close_pair_last_seen[pair] = now

        for vid in pair:
            node = cache.get(vid, {}).get("current_node")
            if node:
                self.memory.hot_nodes[node] = now

        return {"count": count, "age": now - first_seen}

    def _is_sustained_conflict(self, pair_memory: dict[str, float | int]) -> bool:
        return (
            int(pair_memory.get("count", 0)) >= self.SUSTAINED_CONFLICT_TICKS
            or float(pair_memory.get("age", 0.0)) >= self.SUSTAINED_CONFLICT_SECONDS
        )

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

    def _legal_anchor(self, vehicle: dict) -> tuple[Optional[str], list[list[float]]]:
        """Return the graph node where a new route may legally branch.

        If the vehicle is already moving, the only road-safe first segment is
        continuing to the current path target. Branching from the nearest node
        can create a visual shortcut across grass because the server moves
        linearly between command path points.
        """
        pos = vehicle.get("position")
        if not pos:
            return None, []

        preview = vehicle.get("path_preview") or []
        if vehicle.get("status") == "moving" and preview:
            anchor_point = preview[0]
            anchor_node = self._node_from_position(anchor_point)
            if not anchor_node:
                return None, []
            if self._points_close(pos, anchor_point):
                return anchor_node, [anchor_point]
            return anchor_node, [pos, anchor_point]

        anchor_node = self._node_from_position(pos)
        if not anchor_node:
            return None, []
        node_points = self.sdk.nodes_to_points([anchor_node])
        if node_points and not self._points_close(pos, node_points[0]):
            return anchor_node, [pos, node_points[0]]
        return anchor_node, node_points or [pos]

    def _command_from_node_path(
            self,
            vehicle: dict,
            node_path: list[str],
            action: dict | None,
            speed: float) -> Optional[dict]:
        if not node_path or not self._is_legal_node_path(node_path):
            return None

        anchor_node, prefix = self._legal_anchor(vehicle)
        if not anchor_node:
            return None

        if node_path[0] != anchor_node:
            node_path = [anchor_node] + node_path
        if not self._is_legal_node_path(node_path):
            return None

        suffix = self.sdk.nodes_to_points(node_path)
        points = self._merge_points(prefix, suffix)
        if len(points) < 2:
            return None
        return {"path": points, "action": action, "speed": speed}

    def _anchored_preview_path(self, vehicle: dict) -> list[list[float]]:
        pos = vehicle.get("position")
        preview = vehicle.get("path_preview") or []
        if not pos or not preview:
            return []
        return self._merge_points([pos], preview)

    def _is_legal_node_path(self, node_path: list[str]) -> bool:
        if len(node_path) < 2:
            return True
        for left, right in zip(node_path, node_path[1:]):
            if left == right:
                continue
            neighbors = {n for n, _weight in self.sdk._adjacency.get(left, [])}
            if right not in neighbors:
                return False
        return True

    def _merge_points(self, first: list[list[float]],
                      second: list[list[float]]) -> list[list[float]]:
        points: list[list[float]] = []
        for point in (first or []) + (second or []):
            if not point:
                continue
            if points and self._points_close(points[-1], point):
                continue
            points.append([float(point[0]), float(point[1])])
        return points

    @staticmethod
    def _points_close(a: list, b: list, eps: float = 1e-6) -> bool:
        if not a or not b:
            return False
        return abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) <= eps

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

    def _target_staging_command(self, vehicle: dict, task: Task, ctx: dict,
                                vehicle_id: str | None) -> Optional[dict]:
        target_zone = task.target_zone
        if not target_zone or not self.sdk:
            return None

        zone = ctx.get("zones", {}).get(target_zone, {})
        if zone.get("type") != "processing":
            return None

        zone_pos = self.sdk.get_zone_position(target_zone)
        target_node = self.sdk.get_zone_node(target_zone)
        pos = vehicle.get("position")
        if not zone_pos or not target_node or not pos:
            return None

        # If the vehicle is already close enough, let the normal command carry
        # the interaction action so pick/drop can complete.
        if self._distance(pos, zone_pos) <= self.IDLE_CLEAR_DISTANCE:
            return None
        if not self._target_physically_busy(target_zone, target_node, ctx, vehicle_id):
            return None

        start, _prefix = self._legal_anchor(vehicle)
        staging = self._staging_node_for_target(target_node, ctx, vehicle_id)
        if not start or not staging:
            return None
        if start == staging:
            return {"path": [], "action": None, "speed": 0.0}

        node_path = self.sdk.plan_path(start, staging)
        command = self._command_from_node_path(
            vehicle, node_path, None, self.CRUISE_SPEED
        ) if node_path else None
        if not command:
            return None

        self.memory.reserved_staging_nodes.add(staging)
        self.logger.log_event_throttled(
            "v4_target_staging",
            key=(vehicle_id, target_zone, staging),
            min_interval=self.V4_EVENT_LOG_INTERVAL,
            state_or_ctx=ctx,
            vehicle_id=vehicle_id,
            target_zone=target_zone,
            target_node=target_node,
            staging_node=staging,
            task_kind=task.kind,
        )
        return command

    def _target_physically_busy(self, target_zone: str, target_node: str,
                                ctx: dict, vehicle_id: str | None) -> bool:
        zone_pos = self.sdk.get_zone_position(target_zone)
        if not zone_pos:
            return False
        for other, other_vehicle in ctx.get("vehicles", {}).items():
            if other == vehicle_id:
                continue
            if other_vehicle.get("status") != "idle":
                continue
            other_pos = other_vehicle.get("position")
            if not other_pos:
                continue
            other_node = self._node_from_position(other_pos)
            if other_node == target_node and self._distance(other_pos, zone_pos) <= self.TARGET_STAGING_DISTANCE:
                return True
            if self._distance(other_pos, zone_pos) <= self.IDLE_CLEAR_DISTANCE:
                return True
        return False

    def _staging_node_for_target(self, target_node: str, ctx: dict,
                                 vehicle_id: str | None) -> Optional[str]:
        occupied = set(self.memory.reserved_staging_nodes)
        occupied.update(self.memory.reserved_clearance_nodes)
        occupied.update(self.memory.reserved_predispatch_nodes)
        for other, vehicle in ctx.get("vehicles", {}).items():
            if other == vehicle_id:
                continue
            node = self._node_from_position(vehicle.get("position"))
            if node:
                occupied.add(node)
            for point in (vehicle.get("path_preview") or [])[:1]:
                next_node = self._node_from_position(point)
                if next_node:
                    occupied.add(next_node)

        candidates = []
        for neighbor, _weight in self.sdk._adjacency.get(target_node, []):
            if neighbor in occupied or neighbor == target_node:
                continue
            point = self.sdk.nodes_to_points([neighbor])
            if not point:
                continue
            distances = [
                self._distance(point[0], v.get("position"))
                for vid, v in ctx.get("vehicles", {}).items()
                if vid != vehicle_id and v.get("position")
            ]
            min_dist = min(distances) if distances else float("inf")
            hot_penalty = 5.0 if neighbor in self.memory.hot_nodes else 0.0
            candidates.append((min_dist - hot_penalty, neighbor))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

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
            if self._is_replan_protected(vid, ctx, cache, expected_gains):
                continue
            candidates.append(vid)

        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        if self.random_replan_enabled and random.random() < self.RANDOM_REPLAN_PROB:
            return random.choice(candidates)
        return min(candidates, key=lambda vid: (expected_gains[vid], vid))

    def _choose_same_direction_follower(self, a: str, b: str, ctx: dict,
                                        expected_gains: dict[str, float],
                                        cache: dict[str, dict]) -> Optional[str]:
        candidates = []
        for vid in (a, b):
            vehicle = ctx["vehicles"].get(vid, {})
            if vehicle.get("status") != "moving":
                continue
            if self._in_cooldown(vid, ctx):
                continue
            if cache.get(vid, {}).get("current_node") == cache.get(vid, {}).get("next_node"):
                continue
            active = self.memory.active_tasks.get(vid)
            no_task_bonus = -50.0 if not active else 0.0
            protected_bonus = 100.0 if active and self._is_replan_protected(vid, ctx, cache, expected_gains) else 0.0
            candidates.append((expected_gains.get(vid, 0.0) + protected_bonus + no_task_bonus, vid))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

    def _is_replan_protected(self, vid: str, ctx: dict, cache: dict[str, dict],
                             expected_gains: dict[str, float]) -> bool:
        vehicle = ctx["vehicles"].get(vid, {})
        active = self.memory.active_tasks.get(vid)
        task = active.task if active else None
        carrying = vehicle.get("carrying")
        if carrying in ctx.get("prod_items", set()):
            return True
        if task and task.kind in {TaskKind.DROP_PRODUCT, TaskKind.PICK_PRODUCT}:
            return True
        if task and task.target_zone:
            zone_pos = self.sdk.get_zone_position(task.target_zone)
            pos = cache.get(vid, {}).get("pos")
            if zone_pos and pos and self._distance(pos, zone_pos) <= self.COLLISION_WARN_DISTANCE:
                return True
        return expected_gains.get(vid, 0.0) >= self.DEFAULT_PRODUCT_REWARD

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
            if ctx["time"] - self.memory.last_clearance_time.get(vid, -1e9) < self.CLEARANCE_COOLDOWN_SECONDS:
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
        vehicle = ctx["vehicles"].get(vid, {})
        start, _prefix = self._legal_anchor(vehicle)
        avoid_nodes = self._occupied_target_nodes(ctx)
        target = target_node or self._safe_neighbor_node(
            vid,
            ctx,
            cache,
            reserved_nodes=self.memory.reserved_clearance_nodes,
            avoid_nodes=avoid_nodes,
        )
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
        command = self._command_from_node_path(vehicle, path, None, self.CLEARANCE_SPEED)
        if not command:
            self._log_replan_skip(
                "v4_replan_skip",
                ctx,
                vehicle_id=vid,
                other_id=other,
                reason="illegal_clearance_path",
                start=start,
                target=target,
                path_nodes=path,
            )
            return None
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
        self.memory.reserved_clearance_nodes.add(target)
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

        start, _prefix = self._legal_anchor(vehicle)
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
            baseline = self.sdk.plan_path(start, end)
            baseline_distance = self.sdk.path_distance(baseline) if baseline else float("inf")
            path_distance = self.sdk.path_distance(path)
            if baseline_distance > 0 and path_distance > baseline_distance * self.DETOUR_RATIO_LIMIT:
                self._log_replan_skip(
                    "v4_replan_skip",
                    ctx,
                    vehicle_id=vid,
                    other_id=other,
                    reason="detour_too_long",
                    start=start,
                    end=end,
                    blocked_nodes=sorted(blocked),
                    path_distance=round(path_distance, 3),
                    baseline_distance=round(baseline_distance, 3),
                    detour_ratio=round(path_distance / baseline_distance, 3),
                )
                return self._speed_stagger_vehicle(vid, other, ctx, task)
            command = self._command_from_node_path(
                vehicle,
                path,
                {"type": action_type, "target_zone": target_zone},
                self.CRUISE_SPEED,
            )
            if not command:
                self._log_replan_skip(
                    "v4_replan_skip",
                    ctx,
                    vehicle_id=vid,
                    other_id=other,
                    reason="illegal_blocked_replan_path",
                    start=start,
                    end=end,
                    path_nodes=path,
                )
                return self._speed_stagger_vehicle(vid, other, ctx, task)
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
                path_distance=round(path_distance, 3),
            )
            self.logger.log_command(ctx, vid, command, task=task, source="v4_replan")
            return command

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
            path_distance=None,
        )
        return self._speed_stagger_vehicle(vid, other, ctx, task)

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
        path = self._anchored_preview_path(vehicle)
        if not path:
            return None
        command = {
            "path": path,
            "action": {"type": task.action_type, "target_zone": task.target_zone},
            "speed": self.SAME_DIRECTION_SPEED,
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

    def _speed_stagger_no_task_vehicle(self, vid: str, other: str,
                                       ctx: dict) -> Optional[dict]:
        vehicle = ctx["vehicles"].get(vid, {})
        path = self._anchored_preview_path(vehicle)
        if not path:
            return None
        command = {
            "path": path,
            "action": None,
            "speed": self.SAME_DIRECTION_SPEED,
        }
        self.logger.log_event(
            "v4_replan",
            ctx,
            vehicle_id=vid,
            other_id=other,
            target_zone=None,
            action_type=None,
            mode="speed_stagger_no_task",
            fallback=True,
            blocked_nodes=[],
            reason="same_direction_no_active_task",
            path_distance=round(self.sdk.points_distance(path), 3),
        )
        self.logger.log_command(ctx, vid, command, source="v4_speed_stagger")
        return command

    def _safe_neighbor_node(self, vid: str, ctx: dict,
                            cache: dict[str, dict],
                            reserved_nodes: set[str] | None = None,
                            avoid_nodes: set[str] | None = None) -> Optional[str]:
        vehicle = ctx["vehicles"].get(vid, {})
        current, _prefix = self._legal_anchor(vehicle)
        current = current or cache.get(vid, {}).get("current_node")
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
            hot_penalty = 5.0 if neighbor in self.memory.hot_nodes else 0.0
            candidates.append((min_dist - hot_penalty, neighbor))
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
