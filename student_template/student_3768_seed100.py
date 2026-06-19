# Copyright 2026 中山大学智能工程学院谭晓军教授课题组
# SPDX-License-Identifier: Apache-2.0

"""VN 协同调度版本。

核心思路：
1. 集中式任务分配：同一 tick 统一考虑订单、加工区缺料、在途货物和车辆位置。
2. 目标占用：同一原料区/加工区/消费区只派一辆车进入，减少取放货区域碰撞。
3. 道路占用：把路径上的无向边视为互斥资源，尽量绕开已占用道路。
4. 动态重规划：车辆空闲后重新根据最新状态选择目标，所有动作都指定 target_zone。
"""

import heapq
import math
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sdk.agent_sdk import AgentSDK

SERVER_URL = "ws://localhost:8765"
sdk = AgentSDK(SERVER_URL)


PRODUCT_VALUE = {
    "B1": 150,
    "B2": 110,
    "B3": 95,
    "B4": 130,
    "B5": 90,
}
SPECULATIVE_LIMIT = {
    "B1": 99,
    "B2": 99,
    "B3": 99,
    "B4": 99,
    "B5": 99,
}

ASSIGNMENTS = {}
LAST_LOG_TIME = -999.0
# Baked params from best_state.best_seed100:
# seed100=3705.80, mean=4083.49, max=4844.93
MAX_ACTIVE_VEHICLES = 10
TRAVEL_SPEED = 20.0
TIME_SLOT = 0.5
NODE_WINDOW = 1.0
BASE_ALPHA = 8.0
NODE_CONFLICT_LIMIT = 3.0
CRITICAL_DEADLINE = 40.0
NORMAL_DEADLINE = 80.0
PARK_MIN_DIST = 15.0
EVICT_RADIUS = 4.0
RESERVATION_HORIZON = 30.0
PARKING_CACHE_KEY = None
B4_EXTRA_PRIORITY = 380.0
ACTIVE_ORDER_BONUS = 220.0
B4_SPECULATIVE_BONUS = 280.0
SPEC_LIMIT_DEFAULT = 99.0
SPEC_LIMIT_B4 = 1.0
ABANDON_EXTRA_PRODUCTS = 1.0
ABANDON_KEEP_B4 = 2.0
ABANDON_KEEP_OTHER = 1.0
HOTSPOT_REPEAT_THRESHOLD = 2.0
HOTSPOT_ACTIVE_LIMIT = 9.0
PARKING_NODES = [
    "n104",
    "n21",
    "n153",
    "n30",
    "n41",
    "n38",
    "n93",
    "n105",
    "n57",
    "n121",
]


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def zone_product(zone):
    outputs = zone.get("outputs") or []
    return outputs[0] if outputs else None


def nearest_zone_id(position, zones, max_dist=3.2):
    best_id = None
    best_dist = max_dist
    for zid, z in zones.items():
        d = dist(position, z["position"])
        if d <= best_dist:
            best_id = zid
            best_dist = d
    return best_id


def point_to_node(point):
    return sdk.find_nearest_node(point[0], point[1])


def edge_key(a, b):
    return tuple(sorted((a, b)))


def slot_time(value):
    return round(round(value / TIME_SLOT) * TIME_SLOT, 2)


def time_slots(start, end, step=TIME_SLOT):
    t = slot_time(start)
    last = slot_time(end)
    while t <= last + 1e-9:
        yield round(t, 2)
        t += step


def node_pos(node_id):
    node = sdk._nodes.get(node_id)
    if not node:
        return None
    return [node["x"], node["y"]]


def node_distance(a, b):
    pa = node_pos(a)
    pb = node_pos(b)
    if not pa or not pb:
        return 0.0
    return dist(pa, pb)


def reserve_path(vid, nodes, edge_res, node_res, now, speed):
    """Reserve directed edges and node arrival windows for a graph-node path."""
    if not nodes:
        return
    t = now
    for slot in time_slots(t - 0.5, t + 0.5):
        node_res[(nodes[0], slot)] = vid
    for prev, curr in zip(nodes, nodes[1:]):
        travel = node_distance(prev, curr) / max(1.0, speed)
        arrive = t + travel
        for slot in time_slots(t, arrive):
            edge_res[(prev, curr, slot)] = vid
        for slot in time_slots(arrive - 0.5, arrive + 0.5):
            node_res[(curr, slot)] = vid
        t = arrive


def plan_with_reservations(start_node, end_node, edge_res, node_res, now, speed, critical=False):
    """Dijkstra with directed edge and node time-window reservations."""
    if not start_node or not end_node:
        return []
    if start_node == end_node:
        return [start_node]

    start_key = (start_node, slot_time(now))
    heap = [(0.0, now, start_node)]
    prev = {start_key: None}
    best = {start_key: 0.0}
    end_key = None

    while heap:
        cost, arrive, node = heapq.heappop(heap)
        state_key = (node, slot_time(arrive))
        if cost > best.get(state_key, float("inf")) + 1e-9:
            continue
        if node == end_node:
            end_key = state_key
            break
        if arrive - now > RESERVATION_HORIZON:
            continue

        for nxt, weight in sdk._adjacency.get(node, []):
            travel = weight / max(1.0, speed)
            nxt_arrive = arrive + travel

            edge_blocked = False
            for slot in time_slots(arrive, nxt_arrive):
                if edge_res.get((nxt, node, slot)) or edge_res.get((node, nxt, slot)):
                    edge_blocked = True
                    break
            if edge_blocked and not critical:
                continue

            conflicts = 0
            if nxt != end_node:
                for slot in time_slots(nxt_arrive - NODE_WINDOW, nxt_arrive + NODE_WINDOW):
                    if node_res.get((nxt, slot)):
                        conflicts += 1
            if conflicts >= NODE_CONFLICT_LIMIT and not critical:
                continue

            new_cost = cost + weight + BASE_ALPHA * conflicts + (20.0 if edge_blocked else 0.0)
            new_key = (nxt, slot_time(nxt_arrive))
            if new_cost < best.get(new_key, float("inf")):
                best[new_key] = new_cost
                prev[new_key] = state_key
                heapq.heappush(heap, (new_cost, nxt_arrive, nxt))

    if end_key is None:
        return []

    out = []
    key = end_key
    while key is not None:
        out.append(key[0])
        key = prev.get(key)
    return list(reversed(out))


def path_nodes_around(start_node, end_node, forbidden_edges, forbidden_nodes=None):
    """Shortest path with occupied road segments and intersections removed when possible."""
    forbidden_nodes = forbidden_nodes or set()
    if not start_node or not end_node:
        return []
    if start_node == end_node:
        return [start_node]

    heap = [(0.0, start_node)]
    prev = {start_node: None}
    best = {start_node: 0.0}
    visited = set()

    while heap:
        cost, node = heapq.heappop(heap)
        if node in visited:
            continue
        visited.add(node)
        if node == end_node:
            out = []
            while node is not None:
                out.append(node)
                node = prev[node]
            return list(reversed(out))

        for nxt, weight in sdk._adjacency.get(node, []):
            if edge_key(node, nxt) in forbidden_edges:
                continue
            if nxt in forbidden_nodes and nxt != end_node:
                continue
            new_cost = cost + weight
            if new_cost < best.get(nxt, float("inf")):
                best[nxt] = new_cost
                prev[nxt] = node
                heapq.heappush(heap, (new_cost, nxt))

    return sdk.plan_path(start_node, end_node)


def command_to_zone(
    vehicle,
    zone_id,
    action_type,
    blocked_edges,
    blocked_nodes,
    speed=TRAVEL_SPEED,
    vehicle_id=None,
    edge_res=None,
    node_res=None,
    now=0.0,
    critical=False,
):
    zone = sdk.get_zone(zone_id)
    if not zone:
        return None, [], set()

    start = point_to_node(vehicle["position"])
    end = zone.get("node_id")
    if edge_res is not None and node_res is not None:
        nodes = plan_with_reservations(start, end, edge_res, node_res, now, speed, critical)
    else:
        nodes = path_nodes_around(start, end, blocked_edges, blocked_nodes - {start, end})
    if not nodes:
        return None, [], set()

    points = sdk.nodes_to_points(nodes)
    used_edges = {edge_key(a, b) for a, b in zip(nodes, nodes[1:])}
    cmd = {
        "path": points,
        "action": {"type": action_type, "target_zone": zone_id},
        "speed": speed,
    }
    return cmd, nodes, used_edges


def reserve_moving_resources(state, zones):
    """Reserve roads and target areas already used by moving vehicles."""
    reserved_edges = set()
    reserved_nodes = set()
    reserved_zones = set()

    for vid, vehicle in state.get("vehicles", {}).items():
        node = point_to_node(vehicle["position"])
        if node:
            reserved_nodes.add(node)

        preview = vehicle.get("path_preview") or []
        if preview:
            points = [vehicle["position"]] + preview
            nodes = [point_to_node(p) for p in points]
            reserved_nodes.update(n for n in nodes if n)
            for a, b in zip(nodes, nodes[1:]):
                if a and b and a != b:
                    reserved_edges.add(edge_key(a, b))

            target_zone = nearest_zone_id(preview[-1], zones)
            if target_zone:
                reserved_zones.add(target_zone)

        current_zone = nearest_zone_id(vehicle["position"], zones, max_dist=2.0)
        if current_zone and (vehicle.get("status") == "moving" or vehicle.get("carrying")):
            reserved_zones.add(current_zone)

    return reserved_edges, reserved_nodes, reserved_zones


def restore_time_reservations(state):
    edge_res = {}
    node_res = {}
    now = state.get("time", 0.0)
    for vid, vehicle in state.get("vehicles", {}).items():
        points = [vehicle["position"]] + (vehicle.get("path_preview") or [])
        nodes = [point_to_node(p) for p in points]
        nodes = [n for n in nodes if n]
        if nodes:
            reserve_path(vid, nodes, edge_res, node_res, now, vehicle.get("speed") or TRAVEL_SPEED)
    return edge_res, node_res


def precompute_parking_nodes(zones):
    global PARKING_NODES, PARKING_CACHE_KEY
    cache_key = (len(sdk._nodes), len(zones))
    if PARKING_CACHE_KEY == cache_key and PARKING_NODES:
        return PARKING_NODES

    zone_nodes = {z.get("node_id") for z in zones.values()}
    candidates = []
    for nid, edges in sdk._adjacency.items():
        if nid in zone_nodes or len(edges) < 2:
            continue
        pos = node_pos(nid)
        if pos:
            candidates.append((nid, pos))

    selected = []
    center = [sdk._map_width / 2.0, sdk._map_height / 2.0]
    candidates.sort(key=lambda item: dist(item[1], center))
    for nid, pos in candidates:
        if all(dist(pos, node_pos(other)) >= PARK_MIN_DIST for other in selected):
            selected.append(nid)
        if len(selected) >= 14:
            break

    if selected:
        PARKING_NODES = selected
        PARKING_CACHE_KEY = cache_key
    return PARKING_NODES


def vehicle_priority(vid, vehicle, orders, now):
    carrying = vehicle.get("carrying")
    value = 0.0
    urgency = 0.0
    if carrying in PRODUCT_VALUE:
        value = PRODUCT_VALUE[carrying]
        deadlines = [o["deadline"] for o in orders if o["product"] == carrying]
        if deadlines:
            slack = min(deadlines) - now
            urgency = 3.0 if slack < CRITICAL_DEADLINE else (1.0 if slack < NORMAL_DEADLINE else 0.0)
    elif carrying and str(carrying).startswith("A"):
        value = 10.0
        urgency = 1.0
    number = int(vid[1:]) if vid[1:].isdigit() else 99
    return value * 10.0 + urgency * 5.0 - number * 0.01


def active_vehicle_limit(orders):
    if not orders:
        return 7
    counts = Counter(o["product"] for o in orders)
    product, busiest = max(counts.items(), key=lambda item: item[1])
    if product in {"B3", "B4"} and busiest >= HOTSPOT_REPEAT_THRESHOLD:
        return HOTSPOT_ACTIVE_LIMIT
    return MAX_ACTIVE_VEHICLES


def is_safe_parked(vehicle, vehicles, zones):
    node = point_to_node(vehicle["position"])
    if node not in PARKING_NODES:
        return False
    if nearest_zone_id(vehicle["position"], zones, max_dist=3.0):
        return False
    for other in vehicles.values():
        if other is vehicle:
            continue
        if dist(vehicle["position"], other["position"]) < EVICT_RADIUS:
            return False
    return True


def nearest_parking_command(vehicle, vid, vehicles, zones, edge_res, node_res, now, reserved_parking):
    precompute_parking_nodes(zones)
    start = point_to_node(vehicle["position"])
    if not start:
        return None
    occupied = {
        point_to_node(v["position"])
        for oid, v in vehicles.items()
        if oid != vid and v.get("status") == "idle"
    }
    candidates = []
    for target in PARKING_NODES:
        if target in reserved_parking or target in occupied:
            continue
        pos = node_pos(target)
        if pos:
            candidates.append((dist(vehicle["position"], pos), target))
    candidates.sort()
    for _, target in candidates[:6]:
        nodes = plan_with_reservations(start, target, edge_res, node_res, now, 16.0)
        if nodes:
            points = sdk.nodes_to_points(nodes)
            reserve_path(vid, nodes, edge_res, node_res, now, 16.0)
            reserved_parking.add(target)
            return {"path": points, "action": None, "speed": 16.0}, nodes
    return None


def refresh_assignments(state, zones):
    """Drop stale records once a vehicle has become idle or its target disappeared."""
    live = {}
    for vid, rec in ASSIGNMENTS.items():
        vehicle = state.get("vehicles", {}).get(vid)
        if not vehicle:
            continue
        target = rec.get("next") or rec.get("target")
        if target not in zones:
            continue
        if vehicle.get("status") == "moving":
            live[vid] = rec
    ASSIGNMENTS.clear()
    ASSIGNMENTS.update(live)


def in_transit_materials(state):
    counts = Counter()
    for vid, rec in ASSIGNMENTS.items():
        if rec.get("kind") in ("pick_material", "drop_material"):
            vehicle = state.get("vehicles", {}).get(vid)
            if vehicle and vehicle.get("status") == "moving":
                target = rec.get("next") or rec.get("target")
                counts[(target, rec["item"])] += 1
    return counts


def product_orders(orders):
    out = defaultdict(list)
    for order in orders:
        out[order["product"]].append(order)
    for plist in out.values():
        plist.sort(key=lambda o: o["deadline"])
    return out


def best_consumer_for_product(product, position, zones, orders):
    candidates = []
    for order in orders:
        if order["product"] != product:
            continue
        zone = zones.get(order["consumer"])
        if not zone or not zone.get("ready"):
            continue
        urgency = max(1.0, order["deadline"] - (sdk.state or {}).get("time", 0))
        value = PRODUCT_VALUE.get(product, 100)
        score = value * 3.0 / urgency - dist(position, zone["position"]) * 0.04
        candidates.append((score, order["deadline"], order["consumer"]))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def choose_processing_for_material(item, position, zones, orders, transit):
    candidates = []
    active_products = {o["product"] for o in orders}
    for zid, z in zones.items():
        if z.get("type") != "processing" or item not in z.get("inputs", []):
            continue
        needed = z["inputs"].count(item)
        have = z.get("items", {}).get(item, 0) + transit.get((zid, item), 0)
        if have >= needed:
            continue

        product = zone_product(z)
        order_pressure = sum(1 for o in orders if o["product"] == product)
        value = PRODUCT_VALUE.get(product, 100)
        status_bonus = 2.0 if z.get("status") == "idle" else 0.5
        if product in active_products:
            status_bonus += 4.0
        distance_penalty = dist(position, z["position"]) * 0.025
        score = value * 0.02 + order_pressure * 5.0 + status_bonus - distance_penalty
        candidates.append((score, zid))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def build_material_demands(zones, orders, transit):
    demands = []
    active_products = {o["product"] for o in orders}
    deadlines = {}
    active_counts = Counter(o["product"] for o in orders)
    for order in orders:
        deadlines[order["product"]] = min(deadlines.get(order["product"], float("inf")), order["deadline"])

    for zid, z in zones.items():
        if z.get("type") != "processing":
            continue
        product = zone_product(z)
        value = PRODUCT_VALUE.get(product, 100)
        if product == "B4":
            value += B4_EXTRA_PRIORITY
        if product in active_products:
            slack_now = deadlines.get(product, 120.0) - (sdk.state or {}).get("time", 0.0)
            deadline_score = max(0.0, 150.0 - slack_now)
        else:
            deadline_score = 0.0
        active_bonus = ACTIVE_ORDER_BONUS * active_counts.get(product, 0)
        status_bonus = 30.0 if z.get("status") == "idle" else 0.0
        ready_penalty = -40.0 if z.get("ready") else 0.0

        for item in z.get("inputs", []):
            needed = z["inputs"].count(item)
            have = z.get("items", {}).get(item, 0) + transit.get((zid, item), 0)
            if have >= needed:
                continue
            priority = value + active_bonus + deadline_score + status_bonus + ready_penalty
            demands.append((priority, zid, item))

    demands.sort(reverse=True)
    return demands


def choose_raw_pick(vehicle, zones, orders, transit, reserved_zones):
    demands = build_material_demands(zones, orders, transit)
    candidates = []
    for priority, proc_id, item in demands:
        for rzid, rz in zones.items():
            if rzid in reserved_zones:
                continue
            if rz.get("type") != "raw_material" or item not in rz.get("outputs", []):
                continue
            if not rz.get("ready") or rz.get("items", {}).get(item, 0) <= 0:
                continue
            d1 = dist(vehicle["position"], rz["position"])
            d2 = dist(rz["position"], zones[proc_id]["position"])
            candidates.append((priority - d1 * 0.04 - d2 * 0.02, rzid, item, proc_id))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0]


def choose_ready_product_pick(vehicle, zones, orders, reserved_zones, carried_products):
    candidates = []
    pending = product_orders(orders)
    for zid, z in zones.items():
        if zid in reserved_zones or z.get("type") != "processing" or not z.get("ready"):
            continue
        product = zone_product(z)
        value = PRODUCT_VALUE.get(product, 100)
        route = dist(vehicle["position"], z["position"])
        if product in pending:
            order = pending[product][0]
            consumer = zones.get(order["consumer"])
            if consumer:
                route += dist(z["position"], consumer["position"])
            slack = max(1.0, order["deadline"] - (sdk.state or {}).get("time", 0))
            score = value * 2.5 - route * 0.08 + 120.0 / slack
        else:
            limit = SPEC_LIMIT_B4 if product == "B4" else SPEC_LIMIT_DEFAULT
            if carried_products.get(product, 0) >= limit:
                continue
            speculative_bonus = B4_SPECULATIVE_BONUS if product == "B4" else 0.0
            score = value * 0.8 + speculative_bonus - route * 0.05
        candidates.append((score, zid, product))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0]


def park_command(vehicle, vid, blocked_edges, blocked_nodes):
    """Move an idle vehicle to its own waiting node through the road graph."""
    idx = max(0, int(vid[1:]) - 1) if vid[1:].isdigit() else 0
    start = point_to_node(vehicle["position"])
    target = PARKING_NODES[idx % len(PARKING_NODES)]
    if not start or start == target or target in blocked_nodes:
        return None
    nodes = path_nodes_around(start, target, blocked_edges, blocked_nodes - {start, target})
    if not nodes:
        return None
    points = sdk.nodes_to_points(nodes)
    used_edges = {edge_key(a, b) for a, b in zip(nodes, nodes[1:])}
    return {"path": points, "action": None, "speed": 16.0}, nodes, used_edges


def my_strategy(state):
    global LAST_LOG_TIME

    if state.get("type") == "game_over":
        print(f"Final score: {state.get('score', 0):.1f}")
        return {}

    vehicles = state.get("vehicles", {})
    zones = state.get("zones", {})
    orders = sorted(state.get("orders", []), key=lambda o: o["deadline"])
    now = state.get("time", 0.0)
    commands = {}

    refresh_assignments(state, zones)
    reserved_edges, reserved_nodes, reserved_zones = reserve_moving_resources(state, zones)
    edge_res, node_res = restore_time_reservations(state)
    reserved_parking = set()
    precompute_parking_nodes(zones)
    transit = in_transit_materials(state)
    carried_products = Counter(
        v.get("carrying") for v in vehicles.values()
        if v.get("carrying") and str(v.get("carrying")).startswith("B")
    )

    if now - LAST_LOG_TIME >= 20:
        LAST_LOG_TIME = now
        active = sum(1 for v in state.get("vehicles", {}).values() if v.get("status") == "moving")
        print(
            f"[VN] t={now:.0f}s score={state.get('score', 0):.1f} "
            f"orders={len(orders)} active={active} completed={state.get('completed_orders_count', 0)}"
        )

    idle_ids = [vid for vid, v in vehicles.items() if v.get("status") == "idle"]
    idle_ids.sort(key=lambda vid: vehicle_priority(vid, vehicles[vid], orders, now), reverse=True)
    active_count = sum(1 for v in vehicles.values() if v.get("status") == "moving")
    active_limit = active_vehicle_limit(orders)

    for vid in idle_ids:
        vehicle = vehicles[vid]
        carrying = vehicle.get("carrying")
        pos = vehicle["position"]
        own_zone = nearest_zone_id(pos, zones, max_dist=3.0)
        available_reserved_zones = reserved_zones - ({own_zone} if own_zone else set())

        if active_count >= active_limit:
            if not carrying and own_zone:
                parked = nearest_parking_command(vehicle, vid, vehicles, zones, edge_res, node_res, now, reserved_parking)
                if parked:
                    cmd, nodes = parked
                    commands[vid] = cmd
                    reserved_nodes.update(nodes)
                    active_count += 1
            continue

        if carrying and carrying.startswith("A"):
            target = choose_processing_for_material(carrying, pos, zones, orders, transit)
            if not target or target in available_reserved_zones:
                target = None
                for zid, z in zones.items():
                    needed = z.get("inputs", []).count(carrying)
                    have = z.get("items", {}).get(carrying, 0) + transit.get((zid, carrying), 0)
                    if (
                        z.get("type") == "processing"
                        and carrying in z.get("inputs", [])
                        and have < needed
                        and zid not in available_reserved_zones
                    ):
                        target = zid
                        break
            if target:
                cmd, nodes, used = command_to_zone(
                    vehicle,
                    target,
                    "drop",
                    reserved_edges,
                    reserved_nodes,
                    vehicle_id=vid,
                    edge_res=edge_res,
                    node_res=node_res,
                    now=now,
                )
                if cmd:
                    commands[vid] = cmd
                    reserve_path(vid, nodes, edge_res, node_res, now, cmd["speed"])
                    ASSIGNMENTS[vid] = {"kind": "drop_material", "target": target, "item": carrying}
                    reserved_edges.update(used)
                    reserved_nodes.update(nodes)
                    reserved_zones.add(target)
                    transit[(target, carrying)] += 1
                    active_count += 1
            continue

        if carrying and carrying.startswith("B"):
            target = best_consumer_for_product(carrying, pos, zones, orders)
            if (
                ABANDON_EXTRA_PRODUCTS
                and not target
                and carried_products.get(carrying, 0) > (ABANDON_KEEP_B4 if carrying == "B4" else ABANDON_KEEP_OTHER)
            ):
                commands[vid] = {"path": [], "action": {"type": "abandon"}, "speed": 20.0}
                ASSIGNMENTS.pop(vid, None)
                carried_products[carrying] -= 1
                continue
            if target and target not in available_reserved_zones:
                slack = min([o["deadline"] - now for o in orders if o["product"] == carrying] or [999.0])
                cmd, nodes, used = command_to_zone(
                    vehicle,
                    target,
                    "drop",
                    reserved_edges,
                    reserved_nodes,
                    vehicle_id=vid,
                    edge_res=edge_res,
                    node_res=node_res,
                    now=now,
                    critical=slack < CRITICAL_DEADLINE,
                )
                if cmd:
                    commands[vid] = cmd
                    reserve_path(vid, nodes, edge_res, node_res, now, cmd["speed"])
                    ASSIGNMENTS[vid] = {"kind": "deliver_product", "target": target, "item": carrying}
                    reserved_edges.update(used)
                    reserved_nodes.update(nodes)
                    reserved_zones.add(target)
                    active_count += 1
            else:
                parked = park_command(vehicle, vid, reserved_edges, reserved_nodes)
                if parked:
                    cmd, nodes, used = parked
                    commands[vid] = cmd
                    reserved_edges.update(used)
                    reserved_nodes.update(nodes)
                    active_count += 1
            continue

        if carrying:
            commands[vid] = {"path": [], "action": {"type": "abandon"}, "speed": 20.0}
            ASSIGNMENTS.pop(vid, None)
            continue

        product_pick = choose_ready_product_pick(vehicle, zones, orders, available_reserved_zones, carried_products)
        if product_pick:
            _, target, product = product_pick
            cmd, nodes, used = command_to_zone(
                vehicle,
                target,
                "pick",
                reserved_edges,
                reserved_nodes,
                vehicle_id=vid,
                edge_res=edge_res,
                node_res=node_res,
                now=now,
            )
            if cmd:
                commands[vid] = cmd
                reserve_path(vid, nodes, edge_res, node_res, now, cmd["speed"])
                ASSIGNMENTS[vid] = {"kind": "pick_product", "target": target, "item": product}
                reserved_edges.update(used)
                reserved_nodes.update(nodes)
                reserved_zones.add(target)
                carried_products[product] += 1
                active_count += 1
                continue

        raw_pick = choose_raw_pick(vehicle, zones, orders, transit, available_reserved_zones)
        if raw_pick:
            _, target, item, proc_id = raw_pick
            cmd, nodes, used = command_to_zone(
                vehicle,
                target,
                "pick",
                reserved_edges,
                reserved_nodes,
                vehicle_id=vid,
                edge_res=edge_res,
                node_res=node_res,
                now=now,
            )
            if cmd:
                commands[vid] = cmd
                reserve_path(vid, nodes, edge_res, node_res, now, cmd["speed"])
                ASSIGNMENTS[vid] = {
                    "kind": "pick_material",
                    "target": target,
                    "item": item,
                    "next": proc_id,
                }
                reserved_edges.update(used)
                reserved_nodes.update(nodes)
                reserved_zones.add(target)
                transit[(proc_id, item)] += 1
                active_count += 1
                continue

        if nearest_zone_id(pos, zones, max_dist=3.0):
            parked = nearest_parking_command(vehicle, vid, vehicles, zones, edge_res, node_res, now, reserved_parking)
            if parked:
                cmd, nodes = parked
                commands[vid] = cmd
                reserved_nodes.update(nodes)
                active_count += 1

    return commands


if __name__ == "__main__":
    print("=== VN 协同调度策略 ===")
    print("连接到:", SERVER_URL)
    print("策略：订单紧迫度 + 目标占用 + 道路互斥 + 区域避碰")
    print("等待游戏开始...")
    sdk.run(my_strategy)
