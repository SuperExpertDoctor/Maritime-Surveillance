# 持续全域搜索覆盖闭环与实时指标设计

日期：2026-09-17。状态：设计交付，尚未实施。基于代码 `9a0fb2c`；若实施时 HEAD 不同，先按符号核对，不按旧行号机械修改。

配套：[实施任务](../plans/2026-09-17-persistent-coverage-implementation-plan.md)、[验证矩阵](../../validation/2026-09-17-persistent-coverage-validation.md)、[Luna max 执行入口](../plans/2026-09-17-persistent-coverage-luna-handoff.md)。

## 1. 目标、事实与边界

用户要求：纠正 20260917 仿真的区域分配少、搜索执行无效、调度失败及覆盖停滞；全任务海域持续进入动态搜索/重访；用时间窗口内有效区域覆盖面积占比量化；在右侧动态展示；交付足够具体的实施及验证任务，使 Luna max 不必自行猜测跨模块接口。

审计依据：`outputs/diagnostics/coverage_audit_20260917/report.md`、`summary.json` 和两个原始 JSONL。相同 t=177：旧/新累计混合扫描 108/646、22/646；SAR 最近 60 min 53/646、1/646；新日志 500 帧只有 177 个推进分钟，之后 323 帧冻结。前 177 min 只有 UAV-8 真正执行 SAR（39 个 UAV·min 样本）；最后新增格 t=99，最后 SAR t=118，最后任意扫描 t=133。审计数值是现有粗网格下的事实，不是连续成像面积或算法受控比较。

进一步代码证据：

| 位置/符号 | 现状与修改理由 |
|---|---|
| `ControlFactory._create_builtin_heuristic` | CoverageController 使用默认 swath_width=2.0，而环境配置是 15 km / 10 km=1.5 cells；必须统一参数来源 |
| `CoveragePlanner.plan` / `ScanSwath` | 有真实扫描段和左右视方向，控制器只保留 scan_ranges，丢失 look_direction |
| `SimulationEngine._record_control_tick` | SAR 一律采用现有方向或 right，并将航向误差写成 0；不能反映真实扫描几何 |
| `CoverageController._update_phase` | 用短距离下一点的追踪方向判断扫描；长时间 align_scan 已在日志确认，具体跟随根因须闭环复现，不能只改状态名 |
| `_record_search_completion_event` | 将航迹完成当区域完成，并直接写 completion_pct=100 |
| `_enter_emergency_failure` / `_step_controlled_uav` | 标记故障后跳过控制，但可能保留搜索占用和 transit 外观 |
| `TaskAllocator._mission_edges` → `MissionScheduler._prompt_payload` | 先按 utility/urgent 截断几何边，再在剩余池做公平选择，未入边集的海域会被长期饿死 |
| `CandidateExtractor.extract_pool` | ID 包含候选遍历序号；障碍改变可能改变 ID，从而重置候选等待历史 |
| `StateManager.get_coverage_stats` | 只检查 last_scan 有限，SAR/EO 混合；是累计覆盖，不是持续覆盖 |
| `RightSidebar` | 海域覆盖使用累计值；矩阵正值回退不能用于新滑窗指标 |

**不做**：替换 LLM 任务决策为规则代理、放宽飞机物理/安全约束、增大传感器能力以做高成绩、用隐藏船类/真值指导调度、重构无关模块。历史 JSONL 只读。设计文档的数字目标是本次工程验收目标，不宣称已达标。

## 2. 方案选择

| 方案 | 收益 | 局限 | 决策 |
|---|---|---|---|
| A：仅增加滑窗指标和面板 | 快速揭示停滞 | 不修复覆盖能力 | 作为第一阶段，但不足以交付 |
| B：执行/扫描一致性 + 全域待办 + LLM 约束 + 实测指标 | 修复实际闭环，沿用现有架构 | 需要控制、调度、帧、测试共同改动 | **采用** |
| C：全新多机全局覆盖求解器替换现有调度 | 可重设优化目标 | 工程跨度大、破坏模型决策边界，无法由本次日志证明优于 B | 不采用 |

“全域动态划分”采用**逐格覆盖责任 + 动态合法任务矩形**：责任集合覆盖整个固定目标海域，已分配矩形只占当前机队能服务的部分。后台永远能解释每个未新鲜覆盖格是等待、执行中、天气暂缓还是几何不可达；地图无需用假任务框填满。

## 3. 指标的唯一口径

### 3.1 固定目标域、动态可行域

`A`：episode 初始化后冻结的目标海域，来自 `SimulationEngine._intent_searchable_mask()` 同口径：大陆/岛屿/地图外边缘/基地除外，**不随天气缩小**。修改地形须 reset 新 episode；新增/删除船舶不改 A。

`F(t)=A ∩ StateManager.get_searchable_mask()`：此刻规划规则下可搜索区域，仅作辅助分母。天气阻挡是规划限制，不能在传感器实际上有效成像时否认其观测：有效 SAR 足迹可刷新 A 内暂处于 F 外的格，固定主指标照常计入。

格面积 `a=cell_size_km²`。运行时不硬编码 671/646/10/30。零固定域时所有百分比为 null，状态 `no_searchable_area`，UI 显示“无可搜索海域”。

### 3.2 主指标：SAR 有效区域搜索

单独维护 `last_sar_scan_min[c,r]`，初始化为 -inf。仅**实际执行命令允许成像、物理扫描条件有效且 sensor 足迹命中的格**更新；EO、AIS、ESM、预测航迹、任务分配和 renderer cells 都不更新它。

`C_W(t)=100 × count(A ∧ last_sar_scan_min > t-W ∧ last_sar_scan_min ≤ t) / count(A)`。

窗口为左开右闭 `(t-W,t]`；支持且固定提供 `[30,60,120]` min，默认主窗口 60。t<W 时仍以 A 为分母，但 `window_complete=false`，标为“窗口积累中”。t=0 的有效扫描在 t=60 时不属于 60 min 窗口。重复同格、多机重叠不加面积。

辅助指标：固定域累计 SAR 比例、从未扫过比例、已扫但超过主窗口未重访比例、当前动态域的 C_W、暂受天气限制面积。历史 `coverage_pct` 保留原语义且 UI 改称“累计观测覆盖（含光电）”；不得重用字段偷换口径。

SAR 时间必须独立于原 `InformationUpdatePolicy._last_scan_time/_track_scan`：同一格被 EO 观察后，不得覆盖或变更此前 SAR 时间。原 I/V 衰减仍由 InformationUpdatePolicy 负责，不创建另一套 I/V。

### 3.3 时间与统计

唯一时轴为 `sim_time_min`。`CoverageMetrics` 接收有效观测，纯 `snapshot(now_min, feasible_mask)` 不改变状态，不向历史追加采样。episode reset 创建新实例；同时间 frame 重复发布幂等。暂停不推进窗口，也不按浏览器 Date.now 衰减；在运行时状态旁显式标出暂停。

评估采用实际时间加权：对区间 `[max(W,start),end]` 按记录的仿真时间分段积分；测试默认 dt=1 时同时导出逐分钟采样均值用于复核旧审计。报告区分 `sample_mean_pct` 和 `time_mean_pct`，不能互换。P5、最小值和达标时间比只统计完整窗口；缺帧区间未知，不作前向无限填充。未扫描格计入未覆盖比例，不在年龄 P95 中伪装成 0；年龄 P95 仅针对已扫描格并另报 unseen。

### 3.4 能力边界

10 架、160 km/h、15 km 扫幅，在完全无转场/转弯/重叠时，1 h 理想扫过面积上界约 24,000 km²，约占审计固定域 35.8%。因此不能将 C_60=100% 作为此配置的默认承诺。要求是持续改善广域服务、消除无进展和饥饿，而非违反物理上限。

## 4. 数据与接口

新增 `src/mission/coverage_metrics.py`：

```python
class CoverageMetrics:
    def __init__(self, *, episode_id: str, fixed_mask: np.ndarray,
                 cell_size_km: float, windows_min: tuple[int, ...] = (30, 60, 120),
                 primary_window_min: int = 60): ...
    def record_sar(self, cells: tuple[tuple[int, int], ...], *, at_min: float) -> None: ...
    def last_scan_matrix(self) -> np.ndarray: ...  # detached copy
    def snapshot(self, *, now_min: float, feasible_mask: np.ndarray) -> dict: ...
```

接口返回普通 detached JSON 字典，内部 ndarray 不泄漏。record 校验有限非负时间，时间不可倒退（相同时间允许）；格索引必须在地图内，固定域外格忽略。snapshot 不允许早于最后记录时间；不存在历史查询捷径，回放直接使用原帧。JSON 无 NaN/Infinity。

所有新帧在 `include_matrices=False` 时仍包含下面聚合对象，顶层继续 mission-frame/v2，使用独立 schema_version：

```json
{
  "coverage_metrics": {
    "schema_version": "persistent-coverage/v1",
    "episode_id": "episode-...",
    "as_of_min": 177.0,
    "status": "ok",
    "source": "sar",
    "denominator": "fixed_searchable_sea",
    "primary_window_min": 60,
    "fixed_searchable_cells": 671,
    "fixed_searchable_area_km2": 67100.0,
    "currently_searchable_cells": 646,
    "weather_blocked_cells": 25,
    "ever_scanned_cells": 11,
    "cumulative_pct": 1.6393442623,
    "unseen_pct": 98.3606557377,
    "overdue_seen_pct": 1.4903129657,
    "windows": [
      {"minutes": 30, "covered_cells": 0, "covered_area_km2": 0.0,
       "coverage_pct": 0.0, "currently_searchable_coverage_pct": 0.0,
       "window_complete": true},
      {"minutes": 60, "covered_cells": 1, "covered_area_km2": 100.0,
       "coverage_pct": 0.1490312966, "currently_searchable_coverage_pct": 0.1547987616,
       "window_complete": true},
      {"minutes": 120, "covered_cells": 11, "covered_area_km2": 1100.0,
       "coverage_pct": 1.6393442623, "currently_searchable_coverage_pct": 1.7027863777,
       "window_complete": true}
    ]
  }
}
```

此示例由旧审计重建用于说明数值，**不意味着旧帧本来包含该字段**。回放没有该字段时设 null，UI“该回放未记录持续覆盖指标”，不从当帧 I 值猜测。历史重建只用于离线审计，并明确 `reconstructed`，不混入权威实时字段。

`StateManager` 持有 metrics 对象并提供 `configure_coverage_metrics(fixed_mask, episode_id)`、`get_persistent_coverage_stats()`。Simulation 初始化完成地图/基地后配置；实际 SAR 传感器循环负责调用，随后才计算 snapshot。build_frame 只读，不能产生扫描事实。

另外记录每 UAV 每 tick 的 `sar_scan` 审计事件：episode/time/uav_id/task_id/generation/cells，cells 为实际有效足迹（不裁成任务 bbox），同一 tick 同机合并。它是独立复算依据，不是替代统计器。任务验收再与责任域取交集。

## 5. 控制与成像一致性

### 5.1 几何参数和左右视传递

新增 immutable `CoverageExecutionConfig` 放在 `src/control/common/contracts.py`，字段 `swath_width_cells, near_range_cells, min_turn_radius_cells, along_track_cells, heading_tolerance_rad, cross_track_tolerance_cells`。生产通过显式 factory 参数传入，数据来自实际 SARSensor/UAV 的配置；默认测试构造可保留旧默认，但生产构造必须显式。配置中不复制物理扫幅。

`ControlCommand` 增加默认为空的 kw-only `sar_look_direction: Literal['left','right']|None`、`sar_scan_heading_rad: float|None`、`sar_scan_origin: tuple[float,float]|None`；SAR 命令必须具有三个字段，否则 InvalidControlCommand。非 SAR 清空三个字段。安全层 replace() 保留或在关闭 SAR 时清空。训练/自定义控制器同样遵守字段校验，并同步 `src/control/interface.md` 与接口测试。

控制器保存每个 scan_range 对应的 heading/look_direction。航向误差使用**扫描段切线**；不能拿“飞机到一个很近航点的方向”替代条带方向。executor 执行后依据命令中的条带切线和起点重算真实 heading error 与横向误差；不从 controller 私有 route 读取几何。误差>2°、横向误差>0.20 cells 或安全层关闭 SAR 时不能产生扫描记录。`sar_heading_error_deg` 发布实际值，不能写死 0。executor 是成像门控唯一执行者，`_record_control_tick` 不再覆盖其 sar_imaging/方向/误差。

### 5.2 连续跟随与重规划

先用实际 UAVDynamicsExecutor+SafetyEnvelope 闭环重现横/竖、正/反航向和审计中的扫描对准问题。禁止通过逐航点直接设置 UAV 位姿构成唯一成功测试。

仅给 CoverageController 引入 `CoverageRouteFollower`（新文件 `src/control/heuristic/coverage_guidance.py`），其它 probe/track/return 暂不迁移。用沿折线弧长单调前进、局部投影和前视点引导，前视距离 `max(2*v*dt, r_min)`；直线扫描段用段切线+横向误差校正，角速度受现有 action/safety 限制。投影最多搜索从当前进度前后有限弧长邻域，不能跳到相邻平行扫描行。未到最后路径终点不能完成。

每条扫描行有进入/退出稳定段，planner 以实际 footprint 栅格化检验 bbox 是否可覆盖；规划预测只能用于可达性，不能写 metrics。若路线到齐仍漏扫则任务未完成，漏格重新进入全局责任，不靠补写信息修复。

`_refresh_invalidated_route` 验证无冲突后必须恢复 route_status=ready；否则 pending 会使真实路径在新 frame 中消失。每次真正路线改变才增加 route_revision，不能只因天气版本变化清空完整路线。

### 5.3 有界进展看门狗

每个 coverage controller 维护任务级最后有效 route progress、连续 align 时间、重规划次数；到新任务重置，同任务 map version/route revision 不得重置无进展计时。阈值：无弧长进展 10 min、连续 align_scan 8 min、一次任务最多 2 次看门狗重规划（天气必要重规划另计，但不能延长停滞截止）。

第一次到阈值：保持同任务，基于当前位置对未执行扫描段重规划；第二次允许切换扫描方向；第三次 task_failed(reason=coverage_stalled)，经原安全返航/holding 机制释放任务，漏扫责任保留。不得直接把故障机设成可派遣 idle。正常长距离持续转场不触发“无进展”；正常扫描行间转弯与真的 align 停滞需要通过 scan_range 区分。

## 6. 覆盖完成与故障释放

新增 `src/mission/coverage_service.py`，存储 `CoverageTaskProgress(task_id, generation, uav_id, bbox, started_at_min, required_cells, scanned_cells)`。required_cells 是提交时 bbox∩A 的固定集合，后续天气不缩小分母。有效扫描按当前 task/generation 归因，时间不得早于 started_at_min；旧航次扫描和 EO 不计本次完成。

控制器事件改为 `coverage_route_finished`，只说明路线结束。该事件进入 deferred 队列；当 tick 的传感器处理完成后再调用 service 验收。达到 100% required cells 才发布原 search_complete 和 status=completed。否则 status=blocked/release_reason=coverage_incomplete，发布覆盖率、漏格集合摘要，结束当前任务并重新入待办；不得无界延长已批准任务/越过航程预算。候选生成负责漏格小任务，由 LLM 再批准。此策略避免同一任务自行扩张，同时保证漏格不丢失。

更新 StateManager.step：有新进展记录的 region.completion_pct 使用本次任务足迹；历史区域无记录才保留旧计算，且只作兼容。删除直接写 100 的捷径。metrics 累计和 task completion 本来就不同，不要求二者相等。

故障处理必须同步关闭任务记录、region/contact/probe 绑定、控制租约、占区、SAR acquisition 和基地 reservation；重复故障幂等，旧 generation 事件不得释放新任务。新增 coordinator 公共 `quarantine_uav(uav_id, *, current_time, reason)` 原子撤销 controller/lease，UAVState 增加 `operational_status='available'|'failed'`、`failure_reason`；保留历史位置但调度资源列表排除 failed。frame 发布字段，uavDisplayState 显示“故障停用”。不能将冻结当作有效物理飞行，也不能将机数从覆盖分母删除。

## 7. 全域责任、候选公平与资源保障

### 7.1 每格责任

新增 `src/mission/coverage_policy.py`：输入 A、F、last_sar、执行中真实绑定、合法候选池，输出互斥的格分类：fresh、assigned_due、waiting_due、deferred_weather、deferred_geometry。顺序 fresh→天气暂缓→已执行绑定→无合法候选→等待；五者并集=A，交集为空。无有效任务绑定的 active 区域不得占区。几何暂缓不得以“未进入本轮提示窗”冒充不可达。

未覆盖/过期等待年龄由格子键和 last_sar 推导，不能随候选 ID、天气、Prompt 截断重置。普通矩形 ID 改为 `search:c0:r0:c1:r1`；同几何重复任务使用新的 assignment generation 区分，而不是改地理身份。

候选排序：从未扫描格优先；同类别按最老扫描/最长等待；再按需服务格面积/预计总用时（转场+扫描+转弯）排序；最后以 bbox 排序破同分。新鲜区不因 AIS 值高就无限挤占普通海域，目标探查仍单独作为任务类型。

遗漏碎片：对完整普通矩形池并集以外、可规划的 due 格，生成 bbox 面积 1–19 的 fragment 候选；仅当常规池无法服务该格时启用小区，复用完整转弯/障碍/返航可行性检查。同类碎片按最小包围盒递增、无覆盖新格的框去重；任何不满足物理路线条件的格保留 deferred_geometry，不能标 completed 或移出 A。

### 7.2 在几何预筛选之前保障普通搜索

每次 snapshot 先从完整合法池选择一个最多 40 项的候选集合，再为这个同一集合算 feasible_edges，再冻结到 snapshot，最后 Prompt 原样发送；不要在 `_mission_edges` 和 `_prompt_payload` 各自重新截断。ordinary search 保留最少 8 个候选槽位（有多少取多少），其中最老责任格对应候选至少 1 个；剩余槽位由现有 urgent/fair/geography 逻辑填充。普通槽位包含 fragments。

遇到候选几何无边，普通保留槽位沿同排序继续补位，最多检验 120 个候选/轮；未检验格 reason=geometry_budget_deferred，归入 waiting_due，不冒充不可达。下轮从未检验队列继续，不能永远从第一个失败区域开始。

### 7.3 机队保底是约束，不是自动派任务

`min_search_uav_fraction=0.4`。目标 N=ceil(健康机总数×0.4)，未解除的安全返航/加油/正在执行 probe/track 不能抢夺；保底受实际可分配资源和合法路线限制。在新一轮可自由分配资源中，为补齐 N 所需的普通 coverage 留机会。

实现方式：从 ordinary 预留候选中先选**互不重叠**的责任代表框，构建与 available UAV 的可行二分图，最大匹配得到 `feasible_search_slots`。`required_new_search_count=min(max(N-active_healthy_search,0),feasible_search_slots)`。把非重叠代表框 ID 和该下界随 snapshot 送给 LLM；required_new_search_count>0 时还要求包含最老可服务责任格对应的代表任务（must_service_task_ids 单元素），其余由模型选择。validator 同时验证下界、最老责任服务和整体可匹配，防止候选虽曝光却长期不被选。安全/资源不足使下界为 0 时给出 `coverage_floor_infeasible`，不能为了凑数造边。

普通搜索抢占仅在抢占后仍满足可实现保底时允许；新确认目标、handoff 继续按原安全规则处理，已保护任务不被驱逐，实际不足显式记录。失效/卡死任务不算 active_healthy_search。此下界只约束资源，不等价于扫描成功；watchdog 和 C_W 才检验兑现。

LLM 输出无效或少选必须走已有模型纠错/失败流程；后端不偷偷补选任务。无可行搜索时允许 probe 继续利用剩余资源，禁止保底造成全局死锁。

## 8. 模型可靠性

保留生产 30 s 总预算和 0.5 s 后处理预留；不通过单纯增大超时“解决”失败。不宣称因此满足旧文档 2 s 指标。

去掉 Prompt 中未用的完整 contact history：每 contact 使用现有 `select_keypoints` 最多 12 个样本、contact 最多 20 个；候选≤40，feasible edges 使用现有紧凑格式，notes 最多 160 字符，reviewer_summary 最多 1200 字符。完整状态仍保留在本地日志/校验，不把场景真值送入 Prompt。记录 prompt_bytes、snapshot_seconds、prompt_seconds、llm_seconds、validation_seconds、matching_seconds、total_seconds，不能把所有时间伪称 llm_seconds。

同一 snapshot 的模型重试共享 deadline。deadline 过期结果不能安装。连续 3 次 heavyweight decision 失败进入 `paused_model`，blocked_role=decision_maker；可识别的认证/额度耗尽直接暂停。首次/第二次失败保持已提交任务继续，最多每 1 仿真分钟一次，不重复请求同一暂停帧。重试由现有 runtime retry 命令触发，按 blocked_role 分派；decision_maker 重试成功应用 batch 但不推进 clock，失败保持暂停。red_commander 原严格暂停机制保留。

模型额度是外部条件。代码可以正确终止、报告、重试，无法保证缺额度时完成真实模型性能验收。在线验收遇此情况标 BLOCKED，保留证据，不能用 fixture 顶替 PASS。

## 9. 右侧实时展示

位置：RightSidebar 顶部任务概览后、船舶编辑前，新增 `CoveragePanel.jsx`，适配现有 sidebar 宽度/配色，不添加图表依赖。

```text
持续搜索覆盖                 [30] [60] [120] 分钟
最近 60 分钟                       8.20%
[0%                进度条               100%]
已搜索 5,500 / 67,100 km²
SAR 有效扫描 · 固定目标海域
从未搜索 42.1%        超时未重访 49.7%
仿真时刻 03:00:00     窗口已完整
```

示意数值仅演示布局；UI 所有真实数字来自 coverage_metrics。主值 2 位小数，面积中文千分位显示 0 位；0 显示 0.00%，null 显示 `—`。标签“持续搜索覆盖”，tooltip 解释“窗口内至少一次有效 SAR 扫描；重复不重复计面积；不代表每格持续凝视”。

动态刷新：直接读取 App 已选定的 live/replay frame，React 随 WebSocket 帧/回放选帧重新渲染，无新的计时器或轮询。窗口切换只在当前聚合数组选项间切换，不改后端调度窗口；切到新 episode 重置默认 60。暂停显示“模型暂停 · 指标停留在仿真 …”，断连保留最后值但显示“连接中断，非实时”；回放显示“回放数据”。历史缺字段与后端真 0 区分，未知 schema 显示“不支持的指标版本”。

保留现有累计值但更名，信息态势不作为 SAR 指标替代。panel 使用 aria-label、aria-pressed、原生 button、progressbar；小屏不横向溢出、键盘可切换。不要为每帧变化设置高频 aria-live 播报。

## 10. 验收与实施顺序

先指标与复算 → 物理扫描闭环 → 任务验收/故障释放 → 候选/保底/模型可靠性 → 右侧面板 → 系统回归与长时验收。各任务和可运行用例见配套计划/验证矩阵。

合格需要同时满足：计数准确、路径真实、任务完成真实、全域责任可解释、实时显示一致、旧接口兼容、离线全链路指标达标；真实模型达标单独记录。

建议工程门槛已在验证矩阵写定：无天气覆盖场景 480 min 固定域累计 SAR≥80%，720 min≥95%；t=240–720 的 C_60 时间均值≥10%、P5≥5%；有模型输入但无资源/天气硬障碍的例外必须记录、不能修改分母使数字变好。混合场景以统一配置的多种子对照和原日志退化修复双重检查。未通过时保留 FAIL 和原因，禁止下调门槛或直接宣布“全域高效覆盖”。


## 11. 需求到实施的追踪

| 用户问题 / 目标 | 设计落点 | 实施 | 验收 |
|---|---|---|---|
| 不能只看界面，要审查实际覆盖 | 独立SAR扫描时间及原始足迹复算 | T01–T03,T15 | C01–C08,C24,G01 |
| 全域动态覆盖、避免小块反复搜索 | 每格责任、残片、地理身份与最老服务约束 | T09–T11 | C17–C20,G03,G04 |
| 转场/对准但不扫描 | 几何一致、真实动力学、进展看门狗 | T04–T06 | C09–C13,G02 |
| 假完成和故障仍占区 | 本次足迹验收、原子释放/停用 | T07,T08 | C14–C16,G02 |
| 模型超时及额度冻结 | 有界Prompt/真实计时/明确暂停重试 | T12,T13 | C21,C22,G06 |
| 右侧显示并动态刷新 | 当前frame权威聚合，不在浏览器猜测 | T14 | C23,G05 |
| Luna max完整可执行 | 逐任务接口、代码核心、命令、依赖、失败记录 | T01–T16及执行入口 | 全部C/G矩阵 |
