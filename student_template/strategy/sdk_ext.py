"""策略侧增强 SDK。

老师提供的 `AgentSDK` 负责通信和基础寻路。
本类只放本组策略需要的辅助能力，避免直接改动原 SDK 的主体逻辑。
"""

from __future__ import annotations

import math
from typing import Mapping, Optional

from sdk.agent_sdk import AgentSDK


class StrategySDK(AgentSDK):
    """在原 `AgentSDK` 基础上补充策略侧导航辅助方法。"""

    def get_zone_node(self, zone_id: str) -> Optional[str]:
        """获取区域所在的图节点 ID。"""
        info = self._zone_map.get(zone_id)
        if info and info.get("node"):
            return info.get("node")

        zone = self.get_zone(zone_id)
        return zone.get("node_id") if zone else None

    @staticmethod
    def distance(a: list[float], b: list[float]) -> float:
        """计算两个 `[x, y]` 坐标点之间的欧氏距离。"""
        if not a or not b:
            return float("inf")
        return math.hypot(a[0] - b[0], a[1] - b[1])

    def plan_path_with_penalty(
            self,
            start_node: str,
            end_node: str,
            node_penalties: Mapping[str, float] = None) -> list[str]:
        """规划一条带节点惩罚的路径。"""
        if start_node == end_node:
            return [start_node]

        node_penalties = node_penalties or {}
        dist = {start_node: 0.0}
        prev = {start_node: None}
        heap = [(0.0, start_node)]
        visited = set()

        import heapq

        while heap:
            d, u = heapq.heappop(heap)
            if u in visited:
                continue
            visited.add(u)

            if u == end_node:
                path = []
                node = end_node
                while node is not None:
                    path.append(node)
                    node = prev[node]
                return list(reversed(path))

            for neighbor, weight in self._adjacency.get(u, []):
                if neighbor in visited:
                    continue
                penalty = float(node_penalties.get(neighbor, 0.0))
                new_dist = d + weight + penalty
                if new_dist < dist.get(neighbor, float("inf")):
                    dist[neighbor] = new_dist
                    prev[neighbor] = u
                    heapq.heappush(heap, (new_dist, neighbor))

        return []

    def path_distance(self, node_ids: list[str]) -> float:
        """计算一条节点路径的总几何长度。"""
        points = self.nodes_to_points(node_ids)
        return self.points_distance(points)

    def points_distance(self, points: list[list[float]]) -> float:
        """计算一条坐标路径的总几何长度。"""
        if len(points) < 2:
            return 0.0
        return sum(self.distance(points[i - 1], points[i])
                   for i in range(1, len(points)))

    def zone_distance(self, from_position: list[float], zone_id: str) -> float:
        """估计从当前位置到目标区域的最短道路距离。"""
        if from_position is None:
            return float("inf")

        start_node = self.find_nearest_node(from_position[0], from_position[1])
        end_node = self.get_zone_node(zone_id)
        if not start_node or not end_node:
            return float("inf")

        path = self.plan_path(start_node, end_node)
        if not path:
            return float("inf")
        return self.path_distance(path)
