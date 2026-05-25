# 多智能体校园智慧物流协同运输方案设计

本文档基于项目中的 `document.md`、`document_utf8.md`、`server/README.md`、`student_template/README.md`，并结合当前仓库的 `config.json`、`map.json`、`student_template/student.py` 和 `sdk/agent_sdk.py` 梳理。后续汇报可直接以本文档作为方案说明和分工说明的基础。

## 1. 项目目标与问题定义

本实验模拟校园或园区内的无人车协同物流系统。系统中有 10 辆无人车、6 个原料区、5 个加工区和 5 个消费区。无人车需要在共享道路网络上完成两阶段运输：

1. 从原料区取 A 类原料，送到对应加工区。
2. 加工区凑齐配方后生产 B 类成品，无人车再将成品送到有对应订单的消费区。

系统目标不是单车最短路，而是在多辆车共享地图、共享任务和共享道路的条件下，最大化最终得分：

```text
总分 = 订单完成收益 + 原料投递收益 - 碰撞扣分 - 订单超时扣分
```

因此，本问题可以定义为一个带库存、加工时延、订单截止时间和交通冲突约束的多智能体协同调度与路径规划问题。策略需要同时回答两个问题：

- 任务调度：哪辆车在当前时刻应该执行哪个任务，例如取哪种原料、给哪个加工区补料、取哪个成品、服务哪个订单。
- 路径协同：车辆如何到达目标区域，并尽量减少碰撞、道路拥堵、目标点排队和无效里程。

## 2. 当前项目约束核对

### 2.1 地图与车辆

| 项目 | 当前设置 |
| --- | --- |
| 地图范围 | 200 x 200 |
| 道路网络 | `map.json` 中约 148 个节点、219 条双向边 |
| 车辆数量 | 10 |
| 车辆最大速度 | 20 m/s |
| 区域交互半径 | 3.0 m |
| 原料生产周期 | 10 s |
| 原料区最大库存 | 2 |
| 仿真时长 | 300 s |
| 订单检查间隔 | 2 s |
| 订单生成概率 | 1 |
| 订单超时时间 | 80 s |
| 超时扣分 | 0.5 分/s |

### 2.2 配方与收益

| 成品 | 所需原料 | 加工时间/s | 订单价值 |
| --- | --- | ---: | ---: |
| B1 | A1, A2, A4, A6 | 30 | 150 |
| B2 | A1, A2, A4 | 28 | 110 |
| B3 | A3, A5, A6 | 24 | 95 |
| B4 | A1, A5, A6 | 35 | 130 |
| B5 | A2, A3, A4 | 20 | 90 |

### 2.3 文档与配置差异

实验指导书中碰撞扣分描述为每次 10 分，但当前 `config.json` 中 `collision_penalty` 为 5。实际运行时应以服务端读取的 `config.json` 为准，报告中建议说明“本组实验采用仓库当前配置，每次碰撞扣 5 分”。

`server/README.md` 和 `student_template/README.md` 中部分示例仍使用 A1-A4、B1-B3 的旧配置。当前实际地图和 `document.md` 使用 A1-A6、B1-B5，应以 `map.json` 为准。

## 3. 数学建模

### 3.1 图模型

将校园道路网络表示为无向加权图：

```text
G = (N, E, w)
```

其中 `N` 为道路节点集合，`E` 为双向道路边集合，`w(i,j)` 为节点 `i` 和 `j` 间的欧氏距离。SDK 默认用 Dijkstra 算法计算节点间最短路。

区域集合定义如下：

```text
R = {raw_a1, ..., raw_a6}       原料区集合
P = {proc_b1, ..., proc_b5}     加工区集合
C = {cons_c1, ..., cons_c5}     消费区集合
V = {v1, ..., v10}              车辆集合
```

### 3.2 状态变量

在时刻 `t`，系统状态可抽象为：

```text
S(t) = <X(t), L(t), I(t), Q(t), O(t), H(t)>
```

含义如下：

| 符号 | 含义 |
| --- | --- |
| `X(t)` | 所有车辆的位置、速度、路径和运动状态 |
| `L(t)` | 每辆车当前携带物品，空车为 `None` |
| `I(t)` | 原料区、加工区、消费区的库存和就绪状态 |
| `Q(t)` | 加工区的配方、加工状态和剩余加工时间 |
| `O(t)` | 当前未完成订单集合，包括商品类型、消费区和 deadline |
| `H(t)` | 已发生碰撞、超时、完成订单和投递奖励等历史统计 |

### 3.3 决策变量

对每辆车 `v`，每个调度周期需要确定：

```text
a_v(t) = <target_v, action_v, path_v, speed_v>
```

其中：

- `target_v`：目标区域。
- `action_v`：到达目标后执行 `pick`、`drop` 或 `abandon`。
- `path_v`：从当前位置到目标区域的路径点序列。
- `speed_v`：当前速度，用于恢复巡航、排队降速或避碰让行。

对于集中式调度，还可以引入任务分配变量：

```text
x_{v,k}(t) ∈ {0,1}
```

若车辆 `v` 被分配到任务 `k`，则 `x_{v,k}=1`，否则为 0。任务 `k` 可以是“取 A_i”“给 proc_bj 投 A_i”“取 B_j”“给 cons_ck 送 B_j”等。

### 3.4 目标函数

优化目标是在有限仿真时间 `T=300s` 内最大化期望总得分：

```text
max J = Σ completed_order_value
      + Σ material_drop_reward
      - Σ collision_penalty
      - Σ overtime_penalty
```

如果用于策略打分，可以把每个候选任务 `k` 的局部优先级写成：

```text
score(v,k) = α * reward(k)
           + β * urgency(k)
           - γ * distance(v,k)
           - δ * congestion(k)
           - η * conflict_risk(v,k)
```

其中：

- `reward(k)` 表示任务能带来的订单价值或投递收益。
- `urgency(k)` 可由 `1 / max(deadline - current_time, ε)` 表示。
- `distance(v,k)` 表示车辆当前位置到目标区域的最短路距离。
- `congestion(k)` 表示目标区域或路径附近已有车辆数量。
- `conflict_risk(v,k)` 表示路径与其他车辆路径或当前位置的冲突风险。

### 3.5 约束条件

| 约束 | 说明 |
| --- | --- |
| 单车容量约束 | 每辆车同一时刻最多携带 1 件物品 |
| 取放半径约束 | 车辆必须进入区域 3.0 m 交互半径才能完成动作 |
| 原料库存约束 | 只有原料区 `ready=True` 且库存大于 0 时才应派车取货 |
| 加工配方约束 | 加工区只有收齐配方原料后才能开始加工 |
| 成品可取约束 | 只有加工区 `ready=True` 且有对应成品时才派车取成品 |
| 订单匹配约束 | 成品只能投递到需要该成品且订单未完成的消费区 |
| 截止时间约束 | 订单超过 deadline 后持续扣分，应提高优先级 |
| 碰撞约束 | 两车距离小于 2 倍碰撞半径会触发碰撞扣分和冷却 |
| 目标占用约束 | 同一原料、同一成品、同一订单目标不应被多车重复抢占 |

## 4. 基准策略分析

当前 `student_template/student.py` 是典型的分散式贪心策略：

1. 每辆车独立观察当前状态。
2. 只有车辆 `idle` 时才重新分配任务。
3. 携带原料时，送到仍缺该原料的加工区。
4. 携带成品时，送到有对应订单的消费区。
5. 空车优先按订单 deadline 取已完成成品；没有成品可取时，再去取紧急订单需要的原料。

该策略适合作为 V0 基准，因为它使用多辆车，但没有显式车辆协同：

- 不维护全局任务占用表。
- 不避免多车同时前往同一目标。
- 不估计道路拥堵和碰撞风险。
- 不做集中式任务收益排序。
- 不处理车辆到达后目标失效、速度恢复、卡住释放等细节。

已有录像 `recordings/game_20260519_162138.json` 的元数据可作为一次模板策略运行参考：

| 指标 | 数值 |
| --- | ---: |
| 最终分数 | 1576.4 |
| 完成订单数 | 23 |
| 完成订单收益 | 2725.0 |
| 原料投递收益 | 1040.0 |
| 碰撞扣分 | 2110.0 |
| 超时扣分 | 78.6 |

该结果说明模板策略能完成供应链闭环，但碰撞扣分很高，是后续协同优化的主要改进空间。

## 5. 协同策略模块架构

实际实现采用”单一类继承树”结构，每个版本重写自己改动的 1-2 个方法，避免过度抽象。每个 tick 的处理流程由 `__call__` → `_prepare` → `_compute_commands` → 版本特有后处理组成。

### 5.1 整体数据流

```text
state（原始字典）
   │
   v
_prepare(state) → ctx（预计算字典）
   │  预计算: raw_zones, proc_zones, cons_zones,
   │          raw_items, prod_items, pending_orders
   v
memory.prune()  清理过期活动任务
   │
   v
_compute_commands(ctx)
   │
   ├─→ ClaimRegistry.from_memory()  恢复移动中车辆占用
   │
   ├─→ 遍历空闲车辆 (vid 排序)
   │     ├─ _choose_task()         任务选择入口
   │     │    ├─ 携带原料 → _choose_material_drop()
   │     │    ├─ 携带成品 → _choose_product_drop()
   │     │    └─ 空车     → _choose_empty_vehicle_task()
   │     │
   │     ├─ _validate_task()       任务校验（V1 为透传）
   │     └─ _build_command()       生成导航指令
   │
   └─→ registry.claim(task) + memory.active_tasks 登记
   │
   v
版本特有后处理（V4: 时间冲突检测, V5: +安全速度, VN: +超时释放）
   │
   v
commands 输出
```

### 5.2 类继承树

```
V1Strategy
  │  任务入口: _choose_task, _choose_material_drop, _choose_product_drop
  │  空车调度: _choose_empty_vehicle_task, _choose_ready_product_pick
  │  订单排序: _sorted_orders (by deadline)
  │  命令构造: _build_command (navigate_to + target_zone)
  │  辅助方法: _zone_distance
  │
  ├── V2Strategy
  │    重写: _sorted_orders (收益×1.0 + 紧急度×80)
  │    新增: _product_value
  │
  ├── V3Strategy
  │    重写: _choose_empty_vehicle_task (加入前馈补料 fallback)
  │    新增: _choose_forward_fill_task (需求感知补料)
  │
  ├── V4Strategy
  │    重写: __call__ (加入时间冲突检测后处理)
  │    新增: _resolve_time_conflicts, _build_timeline, _find_time_conflict
  │    路径协同: 估算到达时间线，时间重叠的低优先级车减速错峰
  │
  ├── V5Strategy
  │    重写: __call__ (加入极近距离防撞)
  │    新增: _apply_safety_speed, _has_close_vehicle
  │    安全控制: 2m 内低优先级车降速至 14 m/s，安全后恢复 20 m/s
  │
  └── VNStrategy
       重写: __call__ (加入超时任务释放)
       稳定性: 移动中车辆任务超过 45s 未完成则清空路径重新调度
```

### 5.3 核心数据模型

| 类 | 文件 | 职责 |
| --- | --- | --- |
| `Task` | models.py | 单次任务描述：kind、item、pick_zone、drop_zone、priority |
| `ActiveTask` | models.py | 跨 tick 活动任务：绑定车辆、记录分配时间 |
| `StrategyMemory` | models.py | 策略记忆：active_tasks 字典 + low_speed_vehicles 集合 |
| `ClaimRegistry` | registry.py | 目标占用登记：4 个 set 记录已占用的取货/投递目标 |

### 5.4 关键设计决策

1. **不用 WorldView/StrategyConfig**：预计算字段存在普通 dict（ctx）中，版本参数用类常量（如 `CRUISE_SPEED=20.0`）。避免初学者理解 dataclass 的成本。

2. **不用单独 Planner 类**：任务选择方法直接挂在策略类上，V1→V2→V3 通过方法重写演进。减少文件跳转和类树理解成本。

3. **距离排序内置**：所有取货/投递方法都收集候选 → 按 `_zone_distance` 排序 → 选最近。不做复杂的全局距离-收益联合优化。

4. **每个版本只改最少的方法**：V2 只重写 1 个方法，V3 只重写 1 个+新增 1 个，版本差异一目了然。

## 6. 版本功能迭代设计

作业要求至少包含 V0 和 VN 两类控制结构，并插入至少 3 个中间版本。当前方案使用 7 个版本：V0、V1、V2、V3、V4、V5、VN。

| 版本 | 控制结构 | 观察到的问题 | 新增设计 | 预期改进指标 |
| --- | --- | --- | --- | --- |
| V0 | 分散式控制 | 多车独立贪心，重复抢同一目标，碰撞多 | 当前模板策略：单车按携带物和订单 deadline 贪心决策 | 建立基准分数 |
| V1 | 弱集中式（任务） | 多车取同一原料或同一成品，造成排队和空跑 | 增加 `claimed` 目标占用表 + in_transit 在途库存 + 距离排序选最近目标 | 降低无效里程和目标点拥堵 |
| V2 | 集中式任务优先级（任务） | 只按 deadline 导致高价值订单处理不合理 | 引入 `收益×1.0 + 紧急度×80` 综合评分排序 | 提高订单完成收益，降低超时 |
| V3 | 供应链前馈协同（任务） | 只响应已有订单，成品产出慢，车辆等待多 | 有订单需求时才预补料，只补缺失原料 | 提高成品可取率和订单完成数 |
| V4 | 拥堵感知路径规划（路径） | Dijkstra 只看距离最短，无视节点/边上的拥堵 | 对车辆聚集的节点/边加惩罚权重，Dijkstra 自动绕开拥堵路段 | 降低主干道和交叉口碰撞 |
| V5 | 时空联合路径规划（路径） | 只看空间拥堵不够，两车错峰经过同一节点不会冲突 | 估算每辆车到达各节点的时间，检测时间重叠的路径冲突；轻微重叠减速，严重重叠爬行等待 | 精准避免时间维度的路径冲突 |
| VN | 完整协同优化 | 车辆因拥堵长期卡在同一任务 | 移动中车辆任务超过 45s 未完成则清空路径重新调度 | 提高系统稳定性和总分 |

### 6.1 V0：单车最优、无协同基准

设计思路：

- 每辆车只根据自己的状态和全局可见订单做贪心选择。
- 携带原料就送到缺料加工区。
- 携带成品就送到对应订单消费区。
- 空车优先取紧急订单对应的已完成成品，否则去取该订单缺少的原料。

典型成功案例：

- 当车辆数量少、目标区域分散时，能快速完成“取原料-投料-取成品-送订单”的闭环。
- deadline 排序使部分紧急订单能被优先处理。

典型失败案例：

- 多辆车同时选择同一原料区或成品区。
- 某辆车到达时目标已经被其他车辆取走，产生空跑。
- 车辆在狭窄路段和目标区附近频繁碰撞。
- 缺少任务释放机制，目标失效后可能等待或执行低价值动作。

### 6.2 V1：目标占用与在途库存

针对现象：

- 多车重复取同一个原料或成品。
- 多车给同一个加工区送同一种原料，超过实际需求。

解决思路：

- 在每个 tick 内维护 `claimed_targets`。
- 对已被分配的原料区、成品区、订单消费区跳过。
- 统计车辆当前携带物和正在前往的目标，形成 `in_transit`。
- 判断加工区缺料时，把“已在途原料”也算入库存，避免过量补料。

### 6.3 V2：订单收益、距离与紧急度综合排序

针对现象：

- 只按 deadline 排序会忽略订单价值和路程成本。
- 远距离低价值订单可能占用车辆过久。

解决思路：

```text
order_priority = value(product) * 1.0
               + urgency(deadline) * 80
               - estimated_distance * 0.5
```

优先执行分数最高的订单链路。对已超时订单提高 `urgency`，避免继续累积超时扣分。

### 6.4 V3：加工区前馈补料

针对现象：

- 有订单时才开始找原料，生产时间 20-35 秒，容易错过 deadline。
- 加工区没有持续备料，成品可取率低。

解决思路：

- 订单出现后立刻反推需要的成品和配方原料。
- 高价值 B1、B4 的原料优先级高，但也考虑加工时间较短的 B5。
- 当没有紧急订单时，给加工区预存一轮高频配方原料，使成品更早进入加工状态。
- 控制预存上限，避免所有车辆都为同一加工区服务。

### 6.5 V4：拥堵感知路径规划（带惩罚的 Dijkstra）

针对现象：

- V0 录像中碰撞扣分很高（2110），说明路径协同不足。
- 基础 Dijkstra 只看距离最短，10 辆车可能同时涌入同一条主干道。
- 多车在狭窄路段和交叉口频繁碰撞。

解决思路：

核心是在路径规划阶段引入**空间维度的拥堵感知**。在 `sdk_ext.py` 的 `plan_path_with_penalty` 中，对 Dijkstra 算法的节点权重加入动态惩罚：

- **节点拥堵惩罚**：统计每辆车当前位置所在的节点，若某节点有 2 辆以上车，对该节点加惩罚权重（如 +200），后续车辆规划路径时 Dijkstra 自动绕行。
- **边占用惩罚**：读取每辆车的 `path_preview`，对其未来 3 个路径点所在节点加惩罚权重（如 +30），提前预留道路空间。
- **目标节点特殊处理**：不对路径终点（目标区域所在节点）加惩罚，否则车辆永远到不了目标。
- **惩罚权重调优**：惩罚值需要适中——太小绕路效果不明显，太大会导致车辆绕极远的路、增加行驶时间和超时风险。

关键接口（`sdk_ext.py`）：

```python
def plan_path_with_penalty(start_node, end_node, node_penalties):
    """Dijkstra 变体：node_penalties 为 {node_id: extra_weight}，
       在标准边权重之上累加节点惩罚后选路。"""
```

预期效果：
- 多车分散到不同路径，主干道车流更均匀
- 交叉口附近不会同时涌入多辆车
- 碰撞扣分相比 V0-V3 明显下降
- 代价：部分车辆走了稍长的绕行路，但碰撞减少的收益大于绕路成本

### 6.6 V5：时空联合路径规划

针对现象：

- V4 的拥堵感知只看"空间"——哪些节点当前有车。但很多冲突是"时间"维度的：两辆车不同时间经过同一节点并不会碰撞。
- V4 将所有有车的节点一视同仁地惩罚，导致不必要的绕路。例如 A 车 2 秒后到达节点 X，B 车 8 秒后到达——它们根本不会相遇，V4 却让 B 车绕路。
- 反之，两车在 1 秒内同时到达同一节点，V4 的静态惩罚（+200）可能不足以迫使 Dijkstra 绕路（因为绕路可能增加 300+ 距离）。

解决思路：

引入**时间维度**的路径冲突检测，从"空间冲突"升级为"时空冲突"：

1. **构建到达时间线**：为每条路径估算车辆到达各节点的时刻（距离 ÷ 速度）。
2. **检测时空冲突**：若两车到达同一节点的时间差 < 3 秒，判定为冲突。
3. **分级错峰**：按时间重叠程度分级处理：
   - gap < 1 秒：严重冲突 → 低优先级车降至 3 m/s（爬行，等同于在冲突节点前等待）
   - gap < 2 秒：中度冲突 → 降至 10 m/s
   - gap < 3 秒：轻微冲突 → 降至 14 m/s
4. **高优先级车不受影响**：只调整优先级较低的车（按车辆编号排序）。

关键方法（`strategy.py` V5Strategy）：

```python
def _build_timeline(cmd) -> list[(node, arrival_time)]:
    """估算路径上各节点的到达时刻"""

def _find_time_conflict(timeline_a, timeline_b) -> (node, t_a, t_b) | None:
    """找第一个 3s 内的时间重叠"""

def _resolve_time_conflicts(commands):
    """对所有命令逐对检测，低优先级车减速错峰"""
```

与 V4 的关系：
- V4 做空间维度的"粗粒度"拥堵绕行（一个节点有 2+ 车就绕）
- V5 做时间维度的"细粒度"冲突避让（只有同时到达才干预）
- V5 可以替代 V4 的拥堵惩罚，也可以用 V4 的惩罚作为初始路径 + V5 做时间调整，二者互补

### 6.7 VN：完整协同调度优化算法

最终版本整合 V1-V5，补充动态重规划：

- 如果车辆到达目标后目标不再需要当前货物，立即重新选择有效目标。
- 如果成品订单消失或被其他车辆完成，携带成品车辆改送同类其他订单。
- 如果加工区已不缺当前原料，改送其他缺该原料的加工区；若全局都不缺，再考虑丢弃。
- **超时任务释放**：记录车辆任务开始时间，超过预期时间仍未完成则清空路径，下个 tick 重新调度。
- 每次发指令都使用 `target_zone`，防止路过其他区域误触发。

## 7. 实验记录表模板

建议每个版本使用相同 `config.json`、相同随机种子、相同运行时长，每个版本至少运行 3 次，记录平均值和最好值。下面表格先放入已有模板录像的一组数据，其余版本待运行后填写。

| 版本 | 描述现象概述 | 新增设计概述 | 总分 | 完成订单数 | 订单收益 | 投递收益 | 碰撞扣分 | 超时扣分 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| V0 | 无协同，碰撞和重复抢占明显 | 当前模板贪心策略 | 1576.4 | 23 | 2725.0 | 1040.0 | 2110.0 | 78.6 |
| V1 | 重复目标减少 | claimed / in_transit 目标占用 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 |
| V2 | 订单选择更合理 | deadline + 收益/距离优先级 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 |
| V3 | 成品等待减少 | 前馈补料和配方优先级 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 |
| V4 | 主干道碰撞减少 | 带节点/边拥堵惩罚的 Dijkstra 绕行 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 |
| V5 | 路径冲突精准避免 | 时空联合检测 + 分级错峰 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 |
| VN | 稳定性提升 | 超时任务释放 + 动态重规划 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 |

分析口径：

- 如果总分上升但订单数不变，通常说明碰撞或超时下降。
- 如果订单收益上升但总分下降，说明新增策略可能造成碰撞或空驶增加。
- 如果投递收益高但订单收益低，说明原料补给过多，成品配送不足。
- 如果碰撞扣分下降但订单数也下降，说明避碰过于保守，需要提高恢复速度或减少等待。

## 8. 代码结构与接口规范

当前项目保留老师模板入口 `student_template/student.py`，同时新增独立版本文件和可复用策略包，便于做 V0-VN 对比实验。

### 8.1 文件结构（当前实现）

```text
student_template/
  student.py                  老师模板入口，保留为 V0 基准策略
  student_v0.py               V0 入口（同 student.py，分散式贪心）
  student_v1.py               V1 入口：V0 + claimed 目标占用 + 距离排序
  student_v2.py               V2 入口：收益/紧急度综合评分
  student_v3.py               V3 入口：需求感知前馈补料
  student_v4.py               V4 入口：时空路径冲突检测与错峰
  student_v5.py               V5 入口：极近距离避碰降速
  student_vn.py               VN 入口：超时任务释放
  strategy/
    __init__.py               导出 V1~VN 策略类
    models.py                 Task、ActiveTask、StrategyMemory 等数据模型（无 WorldView/StrategyConfig）
    utils.py                  纯函数：车辆排序、订单字段提取、紧急度计算
    sdk_ext.py                StrategySDK：继承 AgentSDK，补充距离查询和加权寻路
    registry.py               ClaimRegistry：目标占用登记 + 在途库存查询
    strategy.py               所有版本策略的单一类继承树（无独立 planner.py）
sdk/
  agent_sdk.py                老师提供的基础 SDK，负责通信、地图读取和基础寻路
```

与初版设计的主要变化：
- **移除 `planner.py`**：任务选择逻辑合并到 `strategy.py` 的类树中，减少文件跳转
- **移除 `WorldView` / `StrategyConfig`**：预计算字段用普通 dict 传递，版本参数用类常量
- **新增 `student_v5.py`**：V4（时空冲突）和 V5（避碰降速）拆为两个独立版本

### 8.2 统一入口规范

所有可运行版本都保持相同入口结构：

```python
from student_template.strategy import V1Strategy  # 替换为对应版本
from student_template.strategy.sdk_ext import StrategySDK

SERVER_URL = "ws://localhost:8765"
sdk = StrategySDK(SERVER_URL)
strategy = V1Strategy(sdk)          # 无需传 config


def my_strategy(state):
    return strategy(state)


if __name__ == "__main__":
    sdk.run(my_strategy)
```

运行方式：

```bash
python student_template/student_v0.py
python student_template/student_v1.py
# ... 依次到 ...
python student_template/student_vn.py
```

### 8.3 核心数据模型

`Task` — 单次任务的最小单元：

```python
@dataclass
class Task:
    kind: TaskKind       # PICK_RAW | DROP_MATERIAL | PICK_PRODUCT | DROP_PRODUCT | ABANDON | WAIT
    item: str | None     # "A1" / "B1" / None
    pick_zone: str | None
    drop_zone: str | None
    order_id: str | None
    priority: float      # 任务优先级，V2 引入
    reason: str          # 调试说明

    # 属性（自动计算）
    target_zone: str     # pick 任务返回 pick_zone，drop 任务返回 drop_zone
    action_type: str     # "pick" / "drop" / "abandon"
```

`ActiveTask` — 跨 tick 保留的活动任务：

```python
@dataclass
class ActiveTask:
    vehicle_id: str
    task: Task
    assigned_at: float          # 分配时间
    start_carrying: str | None  # 分配时携带的物品
```

`StrategyMemory` — 策略持久记忆：

```python
@dataclass
class StrategyMemory:
    active_tasks: dict[str, ActiveTask]    # vid → 活动任务
    low_speed_vehicles: set[str]           # 当前降速车辆

    def prune(self, vehicles, current_time, stale_seconds):
        """清理 idle/过期任务"""
```

`ClaimRegistry` — 目标占用登记表：

| 方法 | 说明 |
| --- | --- |
| `from_memory(memory, vehicles)` | 从记忆恢复移动中车辆的占用状态 |
| `can_claim(task) → bool` | 检查目标是否已被占用 |
| `claim(task)` | 登记目标占用 |
| `material_in_transit(zone_id, item) → int` | 查询某加工区某原料的在途数量（0 或 1） |

### 8.4 预计算上下文（ctx dict）

`_prepare(state)` 方法一次遍历 zone 得到预计算字段，后续所有方法共享：

| 字段 | 来源 | 用途 |
| --- | --- | --- |
| `time` | `state["time"]` | 紧迫度计算 |
| `vehicles` | `state["vehicles"]` | 遍历空闲车辆 |
| `zones` | `state["zones"]` | 查询区域状态 |
| `pending_orders` | `state["orders"]` 过滤 pending | 订单排序 |
| `raw_zones` | type == "raw_material" | 原料取货 |
| `proc_zones` | type == "processing" | 加工投料/成品取货 |
| `cons_zones` | type == "consumer" | 订单投递 |
| `raw_items` | raw_material 的 outputs | 区分原料/成品 |
| `prod_items` | processing 的 outputs | 区分原料/成品 |

### 8.5 版本方法重写一览

| 版本 | 与上一版本的区别 | 重写的方法 |
| --- | --- | --- |
| V1 | + claimed 目标占用 + 距离排序 + target_zone | 全新实现 |
| V2 | + 收益/紧急度综合评分 | `_sorted_orders`, `_order_priority` |
| V3 | + 需求感知前馈补料 | `_choose_empty_vehicle_task` + 新增 `_choose_forward_fill_task` |
| V4 | + 带节点/边拥堵惩罚的 Dijkstra | `_build_command` + 新增 `_build_node_penalties` |
| V5 | + 时空联合冲突检测与分级错峰 | `__call__` + 新增 `_resolve_time_conflicts`, `_build_timeline` |
| VN | + 超时任务释放 | `__call__` |

### 8.6 策略侧增强 SDK

`StrategySDK` 继承 `AgentSDK`，补充策略需要的辅助方法：

| 方法 | 作用 | 使用者 |
| --- | --- | --- |
| `get_zone_node(zone_id)` | 查询区域所在图节点 | V4, V5 |
| `distance(a, b)` | 两坐标点欧氏距离 | V4, V5 |
| `zone_distance(from_position, zone_id)` | 估计到目标区域的道路距离 | V1-V3 距离排序 |
| `plan_path_with_penalty(start, end, penalties)` | 带节点惩罚的 Dijkstra | V4 核心接口 |
| `points_distance(points)` | 坐标路径总长度 | V5 时间线估算 |
| `path_distance(node_ids)` | 节点路径总长度 | V5 时间线估算 |

## 9. 三人分工与个人贡献

最终汇报时可按“算法、工程、实验报告”三条线描述个人贡献。下面使用“成员 A/B/C”占位，提交前替换为真实姓名。

| 成员 | 主要职责 | 具体贡献 | 汇报时可说明的产出 |
| --- | --- | --- | --- |
| 成员 A | 任务调度与数学建模 | 负责问题定义、目标函数、约束建模；实现 V0、V1、V2 的任务优先级和目标占用逻辑 | 建模公式、任务分配流程图、V0-V2 对比分析 |
| 成员 B | 路径协同与避碰控制 | 负责路径规划分析、拥堵检测、局部降速、速度恢复和动态重规划；实现 V4、VN 中的安全控制 | 避碰规则、路径协同模块、碰撞扣分下降数据 |
| 成员 C | 实验评估与报告汇报 | 负责运行各版本实验、整理录像截图和关键日志、汇总表格、分析成功/失败案例；维护最终报告和 PPT | 实验数据表、截图、案例分析、最终汇报材料 |

建议贡献比例：

| 成员 | 建议贡献比例 | 说明 |
| --- | ---: | --- |
| 成员 A | 35% | 核心调度策略和建模部分工作量较大 |
| 成员 B | 35% | 协同避碰和稳定性优化直接影响最终得分 |
| 成员 C | 30% | 实验运行、数据分析、报告和汇报整合不可缺少 |

如果实际实现中某位成员承担了更多代码或实验工作，可在最终报告中调整比例，但应保证每个人都有可验证产出，例如代码片段、实验记录、图表或汇报章节。

## 10. 最终汇报建议结构

1. 实验背景：校园智慧物流供应链，多车协同运输。
2. 问题定义：多智能体任务调度 + 共享道路路径协同。
3. 数学建模：图模型、状态变量、决策变量、目标函数和约束。
4. V0 基准：分散式贪心策略、成功案例、失败案例。
5. 版本迭代：V1-V4 每个版本解决的具体问题。
6. VN 最终算法：集中式调度、目标占用、订单优先级、前馈补料、避碰和动态重规划。
7. 实验结果：同配置下各版本分数、订单数、碰撞扣分、超时扣分对比。
8. 个人贡献：三名成员分别说明负责模块和产出。
9. 总结反思：协同策略相比单车贪心的收益，以及仍可优化的方向。

## 11. 后续待完成事项

| 优先级 | 事项 | 说明 | 状态 |
| --- | --- | --- | --- |
| 高 | 实现 V1 目标占用 | ClaimRegistry + in_transit 在途库存 | ✅ 已完成 |
| 高 | 所有 pick/drop 指令补充 `target_zone` | 防止误触发，符合指导书建议 | ✅ 已完成 |
| 高 | 记录 V1-VN 实验数据 | 每个版本相同配置（--speed 100 --seed 100）运行，保存录像和最终分数 | 🔲 待做 |
| 中 | 实现订单综合评分 | V2: value×1.0 + urgency×80 | ✅ 已完成 |
| 中 | 实现前馈补料 | V3: 需求感知补料 | ✅ 已完成 |
| 中 | 实现路径协同（空间） | V4: 带节点/边拥堵惩罚的 Dijkstra | 🔲 待实现 |
| 中 | 实现路径协同（时空） | V5: 时空联合冲突检测 + 分级错峰 | 🔲 待实现 |
| 中 | 实现卡住任务释放 | VN: 超时 45s 清空路径 | ✅ 已完成 |
| 中 | 调优 V4 惩罚权重 | 惩罚值需适中，避免绕路过远 | 🔲 待做 |
| 低 | 美化实验数据表格 | 填入 3 次运行数据，计算平均值和最佳值 | 🔲 待做 |
| 低 | 撰写最终报告 | 按第 10 节建议结构组织 | 🔲 待做 |
