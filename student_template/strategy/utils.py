"""策略层的小型辅助函数。"""

from __future__ import annotations

import re
from typing import Any


def vehicle_sort_key(vehicle_id: str) -> tuple[int, str]:
    match = re.search(r"\d+", vehicle_id)
    return (int(match.group(0)) if match else 10_000, vehicle_id)


def order_deadline(order: dict[str, Any]) -> float:
    return float(order.get("deadline", float("inf")))


def order_product(order: dict[str, Any]) -> str:
    return order.get("product") or order.get("required") or ""


def order_consumer(order: dict[str, Any]) -> str:
    return order.get("consumer") or order.get("consumer_id") or ""


def order_id(order: dict[str, Any]) -> str:
    return order.get("id") or order.get("order_id") or ""


def urgency_score(current_time: float, deadline: float) -> float:
    remaining = max(deadline - current_time, 1.0)
    return 1.0 / remaining


def hungarian_assign(cost_matrix: list[list[float]]) -> list[int]:
    """匈牙利算法：n×m 代价矩阵，返回每行分配到的列索引（-1 表示未分配）。

    用于 V3 的车辆-任务全局最优匹配。时间复杂度 O(n²m)。
    """
    n = len(cost_matrix)
    if n == 0:
        return []
    m = max(len(row) for row in cost_matrix) if cost_matrix else 0
    if m == 0:
        return [-1] * n

    # 补成方阵，填充大值
    size = max(n, m)
    INF = 1e12
    a = [row[:] + [INF] * (size - len(row)) for row in cost_matrix]
    if n < size:
        for _ in range(size - n):
            a.append([0.0] * size)

    u = [0.0] * (size + 1)
    v = [0.0] * (size + 1)
    p = [0] * (size + 1)
    way = [0] * (size + 1)

    for i in range(1, size + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            for j in range(1, size + 1):
                if not used[j]:
                    cur = a[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(size + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    assignment = [-1] * size
    for j in range(1, size + 1):
        if p[j] != 0:
            assignment[p[j] - 1] = j - 1
    return assignment[:n]
