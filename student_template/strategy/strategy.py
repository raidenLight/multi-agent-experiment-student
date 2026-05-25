"""可组合的策略实现。"""

from __future__ import annotations

from typing import Optional

from .models import ActiveTask, StrategyConfig, StrategyMemory, Task, TaskKind, WorldView
from .planner import V1TaskPlanner, V2TaskPlanner, V3TaskPlanner
from .registry import ClaimRegistry
from .utils import vehicle_sort_key


class V1Strategy:
    """V1 策略：V0 贪心行为加集中式目标占用。"""

    def __init__(self, sdk, config: StrategyConfig = None) -> None:
        self.sdk = sdk
        self.config = config or StrategyConfig()
        self.memory = StrategyMemory()
        self.planner = V1TaskPlanner(sdk)
        self.assignment_count = 0
        self.rejected_count = 0

    def __call__(self, state: dict) -> dict:
        world = self._build_world(state)
        self.memory.prune(world, self.config.stale_task_seconds)

        registry = ClaimRegistry.from_memory(self.memory, world)
        commands = {}

        for vid in sorted(world.vehicles, key=vehicle_sort_key):
            vehicle = world.vehicles[vid]
            if vehicle.get("status") != "idle":
                continue

            task = self.planner.choose_task(vid, vehicle, world, registry)
            task = self._validate_task(task, vehicle, world)
            if not task:
                continue

            command = self._build_command(vehicle, task)
            if not command:
                self.rejected_count += 1
                continue

            registry.claim(task)
            self.memory.active_tasks[vid] = ActiveTask(
                vehicle_id=vid,
                task=task,
                assigned_at=world.time,
                start_carrying=vehicle.get("carrying"),
            )
            commands[vid] = command
            self.assignment_count += 1

        return self._apply_safety_speed(commands, world)

    def _build_world(self, state: dict) -> WorldView:
        vehicles = state.get("vehicles", {})
        zones = state.get("zones", {})
        orders = state.get("orders", [])

        raw_items = set()
        product_items = set()
        raw_zones = []
        processing_zones = []
        consumer_zones = []

        for zid, zone in zones.items():
            zone_type = zone.get("type")
            if zone_type == "raw_material":
                raw_zones.append(zid)
                raw_items.update(zone.get("outputs", []))
            elif zone_type == "processing":
                processing_zones.append(zid)
                product_items.update(zone.get("outputs", []))
            elif zone_type == "consumer":
                consumer_zones.append(zid)

        return WorldView(
            raw_state=state,
            time=float(state.get("time", 0.0)),
            vehicles=vehicles,
            zones=zones,
            orders=orders,
            raw_items=raw_items,
            product_items=product_items,
            processing_zones=processing_zones,
            raw_zones=raw_zones,
            consumer_zones=consumer_zones,
        )

    def _build_command(self, vehicle: dict, task: Task) -> Optional[dict]:
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
            speed=self.config.cruise_speed,
        )

    def _validate_task(self, task: Optional[Task], vehicle: dict, world: WorldView) -> Optional[Task]:
        """任务生成后的统一校验入口，默认不修改任务。"""
        return task

    def _apply_safety_speed(self, commands: dict, world: WorldView) -> dict:
        if not self.config.enable_safety_speed:
            return commands

        for vid, command in commands.items():
            vehicle = world.vehicles.get(vid)
            if not vehicle:
                continue
            if self._has_close_priority_vehicle(vid, vehicle, world.vehicles):
                command["speed"] = self.config.slow_speed
                self.memory.low_speed_vehicles.add(vid)
            elif vid in self.memory.low_speed_vehicles:
                command["speed"] = self.config.cruise_speed
                self.memory.low_speed_vehicles.discard(vid)
        return commands

    def _has_close_priority_vehicle(
            self,
            vid: str,
            vehicle: dict,
            vehicles: dict[str, dict]) -> bool:
        pos = vehicle.get("position")
        for other_id, other in vehicles.items():
            if other_id == vid:
                continue
            if vehicle_sort_key(other_id) > vehicle_sort_key(vid):
                continue
            if self.sdk.distance(pos, other.get("position")) < self.config.safety_distance:
                return True
        return False


class V2Strategy(V1Strategy):
    """V2 占位：基于 deadline、收益和距离的调度。"""

    def __init__(self, sdk, config: StrategyConfig = None) -> None:
        super().__init__(sdk, config)
        self.planner = V2TaskPlanner(sdk)


class V3Strategy(V2Strategy):
    """V3 占位：前馈补料和产能预测。"""

    def __init__(self, sdk, config: StrategyConfig = None) -> None:
        config = config or StrategyConfig(enable_forward_fill=True)
        super().__init__(sdk, config)
        self.planner = V3TaskPlanner(sdk)


class V4Strategy(V3Strategy):
    """V4 占位：拥堵感知路径和局部速度控制。"""

    def __init__(self, sdk, config: StrategyConfig = None) -> None:
        config = config or StrategyConfig(
            enable_forward_fill=True,
            enable_congestion_penalty=True,
            enable_safety_speed=True,
        )
        super().__init__(sdk, config)

    def _build_node_penalties(self, world: WorldView) -> dict[str, float]:
        """V4 核心扩展点：根据车辆位置和路径预览生成拥堵节点惩罚。"""
        return {}

    def _apply_safety_speed(self, commands: dict, world: WorldView) -> dict:
        """V4 核心扩展点：局部避碰、让行和速度恢复。"""
        return super()._apply_safety_speed(commands, world)


class VNStrategy(V4Strategy):
    """VN 占位：动态重规划和完整协同。"""

    def _validate_task(self, task: Optional[Task], vehicle: dict, world: WorldView) -> Optional[Task]:
        """VN 核心扩展点：处理目标失效、任务释放和携带物改派。"""
        return task
