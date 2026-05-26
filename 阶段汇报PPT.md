# 多智能体校园智慧物流协同运输系统
## 问题建模、系统框架、策略设计与自动化实验评估

> 阶段汇报 | ~20分钟 | [日期]

---

## 第1张 · 封面（1min）

**多智能体校园智慧物流协同运输系统**

问题建模 · 系统框架 · 策略迭代 · 自动化实验评估

---

小组成员：[姓名A] / [姓名B] / [姓名C]
指导教师：谭晓军 教授
课程：多智能体系统

[占位：中山大学logo]

---

## 第2张 · 实验场景与核心挑战（1.5min）

### 场景
校园无人车物流：10辆车在共享道路网上协同完成供应链运输

```
原料区(A1~A6) ──取货──→ 车辆 ──投料──→ 加工区(B1~B5) ──取货──→ 车辆 ──投递──→ 消费区(订单)
```

### 核心目标
$$\text{总分} = \text{订单完成收益} + \text{原料投递奖励} - \text{碰撞扣分} - \text{订单超时扣分}$$

> 不是单车最短路，而是**多车共享地图、共享任务、共享道路下最大化全局得分**

### 两大核心挑战
| 任务调度（Task Coordination） | 路径协同（Path Coordination） |
|---|---|
| 哪辆车何时执行哪个任务？ | 如何到达目标区域？ |
| 取哪种原料？送哪个加工区？ | 怎样减少碰撞、拥堵、空驶？ |
| 取哪个成品？服务哪个订单？ | 同路段多车如何错峰？ |

[占位：场景示意图]

---

## 第3张 · 系统参数与配方（1.5min）

| 参数 | 值 | | 参数 | 值 |
|---|---|---|---|---|
| 地图 | 200×200m, 148节点/219边 | | 车辆 | 10辆, 最大20 m/s |
| 原料区 | 6个 (A1-A6), 生产10s, 库存≤2 | | 交互半径 | 3.0 m |
| 加工区 | 5个 (B1-B5), 加工20-35s | | 碰撞半径 | 1.0 m |
| 消费区 | 5个, 订单间隔2s, 超时80s | | 仿真 | 300s, 30Hz tick |

### 配方

| 成品 | 所需原料 | 加工 | 价值 | | 成品 | 所需原料 | 加工 | 价值 |
|---|---|---|---|---|---|---|---|---|
| B1 | A1,A2,A4,A6 | 30s | 150 | | B4 | A1,A5,A6 | 35s | 130 |
| B2 | A1,A2,A4 | 28s | 110 | | B5 | A2,A3,A4 | 20s | 90 |
| B3 | A3,A5,A6 | 24s | 95 | | | | | |

### 关键约束
单车容量1件 · 取放需在3m内 · 车距<2m触发碰撞(扣5分,冷却1s) · 超时扣0.5分/s

[占位：地图全景截图]

---

## 第4张 · 图模型与状态建模（2min）

### 道路网络 → 无向加权图
$$G = (N, E, w) \qquad |N|=148,\ |E|=219,\ w = \text{欧氏距离}$$

- 6原料区 + 5加工区 + 5消费区 + 10辆车，各绑定一个图节点
- 路径规划：**Dijkstra 最短路**；车辆沿边移动，到目标节点执行 pick/drop

### 状态变量 S(t)
$$S(t) = \langle X, L, I, Q, O, H \rangle$$

| 符号 | 含义 | 示例 |
|---|---|---|
| $X$ | 车辆位置、速度、路径、idle/moving | v1: [10,20], 20m/s, idle |
| $L$ | 携带物品 | None / A1 / B1 |
| $I$ | 区域库存与就绪状态 | raw_a1: A1×2, ready |
| $Q$ | 加工配方与进度 | proc_b1: 缺A2, 加工剩余5s |
| $O$ | 未完成订单 | o1: need B1 → consumer_1, deadline 95s |
| $H$ | 历史统计 | 已完成23单, 碰撞422次 |

### 决策变量 a_v(t) （每 tick 输出）
$$a_v(t) = \langle target\_zone,\ action,\ path,\ speed \rangle$$

- `target_zone`：防止路过其他区域误触发
- `action`：pick / drop / abandon
- `path`：$[[x_1,y_1],[x_2,y_2],...]$ 坐标路径
- `speed`：巡航20 / 降速避碰 / 恢复

[占位：地图拓扑图 + state数据流示意图]

---

## 第5张 · 目标函数与约束（1.5min）

### 全局优化目标
$$\max J = \underbrace{\sum value(o)}_{\text{订单收益}} + \underbrace{\sum drop\_reward}_{\text{投递奖励}} - \underbrace{\sum collision\_penalty}_{\text{碰撞扣分}} - \underbrace{\sum overtime\_penalty}_{\text{超时扣分}}$$

### 局部任务优先级（V2引入）
$$score(v,k) = \alpha \cdot reward(k) + \beta \cdot urgency(k) - \gamma \cdot distance(v,k)$$

- $reward$：订单价值（B1=150, B5=90）
- $urgency = 1/\max(deadline - t, 1.0)$：越临近超时越紧急
- $distance$：车辆当前位置到目标的道路距离

### 约束条件

| 约束 | 说明 |
|---|---|
| 单车容量 | 每车同时最多携带1件物品 |
| 交互半径 | 必须在目标区域3m内才能 pick/drop |
| 库存约束 | 只有 ready=True 且 stock>0 才能取 |
| 配方约束 | 加工区收齐配方原料后才开始加工 |
| 订单匹配 | 成品只能投到需要该成品且未完成的消费区 |
| 目标占用 | 同原料/成品/订单不能被多车重复抢占 |

---

## 第6张 · 系统架构（1.5min）

### 老师提供的实验平台

```
┌──────────────┐  WebSocket  ┌──────────────────┐  WebSocket  ┌──────────────┐
│  student.py  │ ←──────────→│  game_server.py   │←──────────→│  frontend/   │
│  (策略代码)   │  state/cmds │  (消息路由+广播)   │   state    │  (可视化)    │
└──────────────┘             └────────┬─────────┘            └──────────────┘
                                     │ tick 30Hz
                            ┌────────▼─────────┐
                            │  game_engine.py   │
                            │  移动→碰撞→交互   │
                            │  →生产→订单→积分  │
                            └──────────────────┘
```

- **服务端**（不可修改）：game_server / game_engine / models / pathfinding / recorder
- **SDK**（不可修改）：agent_sdk.py — WebSocket通信、地图读取、Dijkstra寻路、区域查询
- **前端**（不可修改）：可视化渲染、录像回放
- **学生端**（唯一可修改）：`student_template/` 下的策略代码

### Tick 循环（30Hz = 每秒30次）
```
车辆移动 → 碰撞检测 → 区域交互(pick/drop) → 原料冷却 → 加工生产 → 订单生成 → 超时扣分 → 积分更新
```

[占位：系统架构流程图]

---

## 第7张 · 我们在老师模板上的架构设计（1.5min）

### 老师给了什么 vs 我们加了什么

```
老师模板（不可修改）                  我们新增（student_template/strategy/）
┌────────────────────┐          ┌──────────────────────────────────┐
│ sdk/agent_sdk.py   │          │ strategy/                        │
│  · WebSocket通信    │          │   ├─ strategy.py  单一类继承树   │
│  · Dijkstra寻路    │  继承    │   ├─ models.py   Task/ActiveTask │
│  · 区域查询        │  ────→   │   ├─ registry.py ClaimRegistry  │
│  · navigate_to()   │          │   ├─ sdk_ext.py  StrategySDK    │
└────────────────────┘          │   ├─ utils.py   纯函数工具       │
                                │   └─ __init__.py                │
                                │                                  │
                                │ student_v0.py … student_vn.py    │
                                │  (各版本入口，统一接口)            │
                                └──────────────────────────────────┘
```

### 三个关键设计

**1. ctx 预计算字典** — `_prepare(state)` 一次遍历，所有方法共享

| 字段 | 来源 | 用途 |
|---|---|---|
| `time` | `state["time"]` | 紧迫度计算 |
| `vehicles` / `zones` / `orders` | 原始state透传 | 全局查询 |
| `raw_zones` / `proc_zones` / `cons_zones` | 按 type 分类 | 任务选择目标池 |
| `raw_items` / `prod_items` | 从 outputs 提取 | 区分原料/成品 |
| `pending_orders` | 过滤 status=pending | 订单排序 |

> 不用 WorldView dataclass，普通 dict 足够，6 个预计算字段替代重复遍历

**2. ClaimRegistry 目标占用表** — 消除多车重复抢目标

| 登记项 | 数据类型 | 防止 |
|---|---|---|
| `raw_pick_zones` | `set[str]` | 多车取同一原料区 |
| `product_pick_zones` | `set[str]` | 多车取同一成品 |
| `product_orders` | `set[(zone, product)]` | 多车投同一订单 |
| `material_drops` | `set[(zone, item)]` | 多车送同原料到同加工区 |

- `from_memory()`：恢复移动中车辆的占用状态
- `material_in_transit(zone, item)`：查询在途原料数（0或1），避免过量派车

**3. StrategySDK + 统一入口** — 不改老师代码，版本切换只需改一行 import

```python
from student_template.strategy import V1Strategy   # 改成V2/V3/...
from student_template.strategy.sdk_ext import StrategySDK

sdk = StrategySDK("ws://localhost:8765")
strategy = V1Strategy(sdk)
def my_strategy(state): return strategy(state)
```

| SDK 新增方法 | 用途 |
|---|---|
| `zone_distance(pos, zone_id)` | 到目标区域的道路距离（V1-V3距离排序） |
| `plan_path_with_penalty(start, end, penalties)` | 带节点惩罚的Dijkstra（V4拥堵绕行） |
| `distance(a, b)` / `points_distance(pts)` | 欧氏距离 / 路径长度（V4-V5时空计算） |

### 每 tick 处理流程
```python
def __call__(self, state):
    ctx = self._prepare(state)           # → ctx字典（上表）
    self.memory.prune(...)               # 清理过期任务
    return self._compute_commands(ctx)   # 遍历空闲车→选任务→注册占用→导航
```

[占位：IDE代码结构截图 / 类继承树UML图]

---

## 第8张 · 版本迭代总览（1min）

### 设计哲学：每个版本只解决一个问题，只重写1-2个方法

| 版本 | 改进 | 类型 | 类树变化 |
|---|---|---|---|
| V0 | 分散贪心基准 | — | 老师模板 |
| V1 | claimed目标占用 + 距离排序 | 任务 | 新增 V1Strategy |
| V2 | 收益×1.0 + 紧急度×80 评分 | 任务 | 重写 `_sorted_orders` |
| V3 | 需求感知前馈补料 | 任务 | 重写 `_choose_empty_vehicle_task` |
| *V4* | *带节点/边惩罚的Dijkstra绕行* | *路径* | *重写 `_build_command`* |
| *V5* | *时空联合冲突检测+分级错峰* | *路径* | *新增 `_resolve_time_conflicts`* |
| *VN* | *超时任务释放+动态重规划* | *融合* | *重写 `__call__`* |

### 类继承树
```
V1Strategy（基础实现）
  └─ V2Strategy（重写 _sorted_orders）
       └─ V3Strategy（重写 _choose_empty_vehicle_task）
```

> 当前阶段成果：V0-V3（任务协同线已完整实现），V4-VN 设计中

[占位：版本演进路线图]

---

## 第9张 · 工程创新——批量自动化测试（1.5min）

### 痛点
手工测试效率极低：启动server→启动student→打开前端→点开始→等300秒仿真→记录分数。7版本×3种子=21次手工操作，易出错且难保证参数一致性。

### 解决方案：`batch_test.py`

**核心能力**

| 功能 | 实现 |
|---|---|
| **进程生命周期管理** | 自动启动/停止 server 和 student 进程，自动清理端口 |
| **高速仿真** | `--speed 1000` 参数，300s仿真约6-9秒跑完（实时300s） |
| **多随机种子** | 支持指定种子列表或 `--runs N` 自动生成，消除单次偶然性 |
| **Viewer自动触发** | 脚本模拟前端连接、发送 `start_game` 后断开，无需浏览器 |
| **指标自动提取** | 从录像 metadata 读取：订单数、收益、投递、碰撞、超时 |
| **录像归档** | 按 `{version}_seed{seed}.json` 命名，便于回放分析 |
| **汇总输出** | 终端打印表格 + 保存 `batch_results.json` |
| **全部版本一键测试** | `python batch_test.py --all --runs 3` |

### 使用方式
```bash
python batch_test.py student_v4              # 单版本（默认3种子）
python batch_test.py student_v4 100 200 300  # 指定种子
python batch_test.py --all --runs 5          # 全部版本各5种子
```

### 输出示例
```
Versions: [v1, v2, v3]  Seeds: [1, 2, 3]  Speed: 1000×
===============================================================
Version    Score  Orders  Value   Drop   Coll    OT  Runs
v1         2850    32    3450   1120   1580   140    3
v2         2910    34    3680   1090   1720   140    3
v3         2780    31    3350   1150   1580   140    3
```

[占位：batch_test 终端运行截图]

---

## 第10张 · V0——分散式贪心基准（1.5min）

### 决策逻辑（最简单的 if-else）
```
携带原料(A1-A6)？→ 送到缺该原料的加工区（drop）
携带成品(B1-B5)？→ 送到有对应订单的消费区（drop）
空车？          → 优先取已完成成品（pick）
                  若无成品→反推缺料→去原料区取原料（pick）
```

### 优点
每车独立决策，代码简洁，能完成"原料→加工→成品→配送"闭环

### 问题（无协同的代价）
- ❌ **重复抢占**：多车同时选同一目标 → 排队空跑
- ❌ **过量补料**：多车给同一加工区送同种原料
- ❌ **碰撞严重**：狭窄路段无避让机制

### 基准数据（V0单次运行）
| 总分 | 订单 | 收益 | 投递 | 碰撞 | 超时 |
|---|---|---|---|---|---|
| **1576.4** | 23 | 2725 | 1040 | **2110** ←主因 | 78.6 |

> 碰撞扣分占总失分的96%，是后续优化的核心方向

[占位：V0运行录像截图/GIF]

---

## 第11张 · V1——目标占用与距离排序（1.5min）

### 核心思路：引入全局占用表，消除重复任务

**ClaimRegistry（4个set）**
| 登记项 | 作用 |
|---|---|
| `raw_pick_zones` | 防止多车取同一原料区 |
| `product_pick_zones` | 防止多车取同一成品 |
| `product_orders` | 防止多车投同一消费区同一产品 |
| `material_drops` | 防止多车送同原料到同加工区 |

**in_transit 在途库存**
- 计算缺料时计入"正在路上"的原料：`current + in_transit < 1`
- 避免重复派车取同一种原料

**距离排序**
- 所有候选目标按道路距离排序，选最近
- 送原料、取成品、取原料都最近优先

### V1 vs V0
| | V0 | V1 |
|---|---|---|
| 目标选择 | 第一个匹配的 | 最近且未被占用的 |
| 重复取货 | 经常发生 | 同tick内消除 |
| 在途库存 | 不考虑 | 计入 |

[占位：V1 vs V0 对比录屏]

---

## 第12张 · V2——综合优先级评分（1.5min）

### 问题
V1 只按 deadline 排序 → 远距离低价值订单挤占近处高价值订单

### 改进：综合评分
$$priority = \underbrace{value(product)}_{\text{B1=150, B5=90}} \times 1.0 + \underbrace{\frac{1}{\max(deadline-t, 1.0)}}_{\text{紧急度}} \times 80$$

- 高价值订单（B1:150分）得到更多资源倾斜
- 超时订单紧急度飙升，不会被饿死
- 近距离目标通过距离排序自然优先

### V2 vs V1
| | V1 | V2 |
|---|---|---|
| 排序依据 | deadline | value×1.0 + urgency×80 |
| 低价值远距订单 | 可能优先 | 高价值订单优先 |
| 超时订单 | 按deadline线性 | 超时后紧急度飙升 |

[占位：V2 vs V1 订单收益对比图]

---

## 第13张 · V3——需求感知前馈补料（1.5min）

### 问题
V2 被动响应：有订单→找原料→加工(20-35s)→取成品。车辆经常在加工区等货

### 改进：前馈补料
```
没有紧急任务（成品可取/原料急需）？
  → 有待处理订单？→ 是 → 预补料（提前取原料送加工区）
                  → 否 → 不补料（避免空跑）
```

### 三道守卫（防止过度补料）
1. `ordered_products` 为空 → ❌ 不补（没需求不制造需求）
2. 加工区产出品不在订单需求中 → ❌ 不补（只补有用的配方）
3. `current + in_transit ≥ 1` → ❌ 不补（已经有了）

### 效果
- ✅ 成品提前进入加工，减少车辆等待
- ✅ 只补有需求的配方，不浪费运力
- ✅ 守卫条件防止盲目补料

[占位：V3 vs V2 成品等待时间对比]

---

## 第14张 · 总结与后续工作（1min）

### 当前成果

| 维度 | 已完成 |
|---|---|
| 问题建模 | 图模型 G(N,E,w)、状态/决策变量、目标函数、9条约束 |
| 系统框架 | 在老师平台之上自主设计了策略模块架构（继承树+ctx+注册表） |
| 策略实现 | V0-V3 任务协同线：占用表→优先级→前馈补料 |
| 工程工具 | 批量自动化测试脚本（batch_test.py）、1000×高速仿真 |

### 后续工作
| 版本 | 内容 | 类型 |
|---|---|---|
| V4 | 带节点/边拥堵惩罚的 Dijkstra 绕行 | 路径协同（空间） |
| V5 | 时空联合冲突检测 + 分级错峰 | 路径协同（时空） |
| VN | 超时任务释放 + 动态重规划 | 融合 |

### 待办
- [ ] 批量运行 V0-V3 实验数据（相同种子，多轮取平均）
- [ ] 实现 V4-V5 路径协同
- [ ] 得分调优与最终报告

---

> 感谢聆听，欢迎提问 🙏
