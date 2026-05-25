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
