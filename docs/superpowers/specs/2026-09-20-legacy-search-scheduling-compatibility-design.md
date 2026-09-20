# 旧版搜索区域调度兼容层设计

## 1. 目标

在不回退当前混合海上监视能力的前提下，恢复提交
`8e86e9915053d3e1eeb4e4d84cc1aa87572b1281` 中稳定的搜索区域保留和空闲 UAV
确定性重指派逻辑。

目标行为是：已批准但尚未完成的搜索区域在 UAV 被 probe、track、返航或故障流程
释放后继续存在；下一架合格的空闲 UAV 优先接手该区域。只有已有区域无法占满可用搜索
资源时，LLM 才补充新区域。

## 2. 保留范围与非目标

以下当前能力必须保留：

- 基于真实 SAR 成像的累计和滚动覆盖指标；
- zone coverage summary、覆盖新鲜度和全域公平排序；
- probe、track、investigation、direction search 和 intent；
- 动态船舶、AIS、证据、分类、handoff 和红方决策；
- ControlCoordinator、控制权租约、固定翼 Hybrid A* 和安全执行；
- 返航、holding、加油、基地容量和故障隔离；
- LLM gateway、reviewer、strategy memory、任务审计、回放和结果评估；
- 连续决策失败后的显式暂停机制。

本设计不恢复旧版控制器、不移除 MissionTaskRecord，也不取消 zone-aware coverage。
它只改变普通搜索区域的生命周期权威、补区时机和指派顺序。

## 3. 已确认根因

当前实现对 `status in {approved, executing}` 且 `assigned_uav_id is None` 的搜索任务
存在冲突解释：

- 覆盖配额不把它计入正在执行的搜索数量；
- 候选生成允许产生新的覆盖任务；
- 校验器仍把它当作 active search，占用空间并拒绝重叠候选；
- Region 将它标为 active，但控制器和 UAV 已无对应绑定。

这会生成不可满足的约束：调度器要求选择一个 `must_service` 新区域，而验证器又因其
与无人执行的保留区域重叠而拒绝。连续重试最终触发 `decision_maker_failed`。

## 4. 方案选择

采用“旧版搜索调度内核 + 当前任务系统适配层”：

- `StateManager.search_regions` 是搜索几何和待办状态的唯一权威；
- `MissionTaskRecord` 保留为调度快照、控制绑定和审计投影；
- `ControlTask` 只表示当前 UAV 正在执行的任务，不决定区域是否继续存在；
- LLM 只补充区域，不重新选择已保留区域；
- pending 区域由确定性匹配器直接分给空闲 UAV。

不采用仅放宽验证器的补丁，因为它无法恢复旧版的稳定区域重用；也不整体移植旧版
TaskAllocator，因为那会破坏当前混合任务和控制器契约。

## 5. 权威状态和投影

### 5.1 搜索区域状态

搜索区域使用以下语义：

| Region 状态 | assigned_uav_id | 含义 |
|---|---|---|
| `active` | UAV ID | 正在执行或驶往区域 |
| `active` | `None` | pending，保留并等待自动重指派 |
| `completed` | `None` | SAR 覆盖验收通过，不再调度 |
| `stale` | `None` | 几何或地图状态失效，不再调度 |

`active + None` 是显式支持的合法状态，不表示任务异常。

### 5.2 MissionTaskRecord 投影

对应规则为：

| Region 状态 | MissionTaskRecord |
|---|---|
| pending | `approved`, `assigned_uav_id=None` |
| assigned | `executing`, `assigned_uav_id=<UAV>` |
| completed | `completed`, `finished_at_min=<time>` |
| stale | `blocked` 或 `cancelled`，附明确 release reason |

被高优先级工作抢占时，记录从 executing 回到 approved；task ID、区域几何、创建时间和
已取得的 SAR 覆盖进度保持不变。

### 5.3 核心不变量

- 每个 UAV 最多拥有一个当前 ControlTask；
- 每个 active 搜索区域最多绑定一个 UAV；
- 每个 executing 搜索记录必须同时具有 operational UAV、Region 绑定和 coverage assignment；
- pending 区域没有 ControlTask，且不占用 UAV 数量；
- pending 区域仍占用地图几何，候选生成不得创建与之重叠的新区域；
- 抢占、返航和可恢复控制故障不得删除未完成区域；
- completed/stale 区域不得再次自动指派。

## 6. 调度数据流

每次 mission 调度按以下顺序运行：

1. 同步并清理已经 completed、stale 或被 track 区域合法取代的搜索区域。
2. 从 `StateManager.search_regions` 收集 pending 区域。
3. 收集合格空闲 UAV，排除 failed、returning、holding、refueling 及已有控制任务者。
4. 使用确定性最小成本匹配给 pending 区域分配 UAV；匹配必须使用当前 feasible edge、
   航程、地图版本、generation 和控制权条件。
5. 原子提交成功后，将 Region 和 MissionTaskRecord 同步为 assigned/executing。
6. 计算仍未使用的可用搜索槽位。
7. 只有槽位大于零且合法候选存在时，构建 LLM 快照，让模型选择需要新增的区域。
8. 新区域必须与所有 assigned 和 pending 区域均不重叠；通过当前原子任务提交路径安装。
9. 模型失败时保留现有区域和已完成的确定性重配，不撤销有效工作。

这恢复旧版“先复用保留区域、再补区”的行为，同时保留当前任务合法性验证。

## 7. 配额和 zone-aware coverage 兼容

`active_search_count` 拆分为两个概念：

- `assigned_search_count`：当前占用健康 UAV 的搜索区域；
- `reserved_search_count`：所有未完成的 assigned + pending 搜索区域。

自适应搜索预算用于确定期望的并行搜索 UAV 数量。新增区域预算为：

```text
required_new_search_count = max(
    0,
    desired_search_count - assigned_search_count - matchable_pending_count,
)
```

pending 区域优先消耗预算。zone quota 和 `must_service_task_ids` 只从“经过全部 active/pending
区域重叠过滤后”的合法新候选中产生。若最老区域已经由 pending 区域承担，则不得再生成
同一区域的 must-service 新任务。

验证器仍拒绝新增搜索之间的重叠，以及新增搜索与任何未完成保留区域的重叠；但它不再
要求模型重新选择 pending 区域。

## 8. 抢占、恢复和异常处理

### 8.1 高优先级任务抢占

probe、track 或 operator intent 合法抢占搜索 UAV 时：

1. 关闭当前 coverage assignment generation；
2. 清除 Region 的 `assigned_uav_id`；
3. Region 保持 `active`；
4. MissionTaskRecord 回到 `approved`；
5. 发布 `mission_task_released`，reason 为 `preempted`；
6. 下一轮先执行 pending 重配。

### 8.2 正常完成

只有当前 SAR completion contract 验收通过后，Region 和 MissionTaskRecord 才进入
completed。路径走完但 SAR 足迹不足时，区域保持 active；系统可以重新规划或交给另一架
UAV，不得把它当成已覆盖。

### 8.3 地图变化和不可行区域

如果地图更新后，任何健康 UAV 都无法到达 pending 区域，区域转为 stale/blocked，并记录
具体原因。此后 zone planner 才能生成不重叠或替代区域。暂时没有空闲 UAV 不构成 stale。

### 8.4 LLM 失败

LLM 失败只阻止新增区域，不回滚已成功的 pending 重配，不清除保留区域。只有确实需要
新增区域且连续模型失败达到配置阈值时，才沿用当前 `paused_model` 行为。

## 9. 组件边界

计划涉及以下现有组件：

- `src/schedule/state_manager.py`：提供 assigned/pending/unfinished 搜索区域查询；
- `src/schedule/task_allocator.py`：在构建新增任务快照前执行 pending 匹配和剩余预算计算；
- `src/mission/mission_scheduler.py`：只验证真正新增的搜索选择，不要求模型选择 pending；
- `src/mission/coverage_policy.py`：让 zone quota 基于过滤后的新候选和剩余槽位；
- `src/env/simulation.py`：统一抢占、重接手、完成和失效时的 Region/record/control 投影；
- `src/mission/contracts.py`：若需要，在快照中明确发布 pending search IDs；保持 schema 向后兼容；
- `src/mission/prompts/mission_scheduler.txt`：明确模型只选择新增任务。

不在本改造中重写导航器、控制器或前端渲染器。

## 10. 测试与验收

### 10.1 单元契约

- 搜索被 probe 抢占后变成 pending，task ID 和 bbox 不变；
- pending 不计入 assigned search 数量，但计入 reserved geometry；
- 有空闲 UAV 时 pending 在调用 LLM 前被重新指派；
- 候选生成和 zone quota 不包含与 pending 重叠的区域；
- `must_service_task_ids` 一定是后端验证可接受的候选；
- LLM 失败不会删除 pending 或已执行区域；
- stale、completed 区域不会被重配。

### 10.2 集成场景

建立固定 seed 的真实引擎场景：

1. 初始十架 UAV 获得稳定搜索区域；
2. 多架 UAV 被 probe/track 抢占；
3. 原区域保持显示且无人绑定；
4. UAV 任务结束或加油返回后自动接手 pending 区域；
5. 不出现 `must_service` 与 `overlapping_active_search` 同时针对同一选择；
6. 无模型失败时运行到配置终点，不在 79 分钟附近暂停；
7. JSONL 中不存在长期 `executing + assigned_uav_id=None`；
8. 覆盖率和区域稳定性不低于旧版演示基线，同时当前混合任务仍实际执行。

### 10.3 回归门槛

- 当前 mission、coverage、control、replay 测试全部通过；
- 新增旧版兼容行为测试；
- 使用 seed 42 运行至少 120 仿真分钟，验证无约束死锁；
- 对比 `8e86e991...` 的区域连续性、空闲时长和有效覆盖趋势；
- `no_safe_recovery_path` 作为独立导航问题记录，不用调度兼容层掩盖。

## 11. 迁移顺序

1. 先建立状态不变量和失败测试，复现当前幽灵 active task 冲突。
2. 增加 pending/assigned 查询和确定性重配服务。
3. 将抢占与任务完成路径统一到新投影规则。
4. 修改新增预算、zone quota 和验证器边界。
5. 更新 prompt，使 LLM 明确只负责新增区域。
6. 跑单元、集成和固定 seed 长时仿真。
7. 最后再评估 UAV-9 的独立 Hybrid A* 恢复失败。

## 12. 完成定义

当搜索 UAV 被高优先级任务抢占时，区域稳定保留；空闲 UAV 无需 LLM 重选即可自动接手；
LLM 只填补真实区域缺口；所有当前新功能和安全约束继续生效；同一次快照不会再产生必须
选择但必然被 `overlapping_active_search` 拒绝的候选。
