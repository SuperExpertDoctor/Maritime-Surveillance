# 海上违法作业船舶搜索与识别增量设计

> 术语已按 2026-09-16 统一：分类使用 I 类船舶/II 类船舶，运行时值使用 `type_i`/`type_ii`。

> 状态：待用户审阅。本文是 `docs/2026-09-15-maritime-requirements.md` 的技术设计草案，不代表任何建议项已获批准，也不授权修改业务代码。

## 1. 目标与范围

本设计在当前 `branch1` 已有的混合海上目标、接触证据、统一任务目录、LLM 选择和确定性任务匹配基础上做增量对齐，目标是：

1. 用统一术语表达I 类船舶、II 类船舶、类别识别和违规作业判定。
2. 将第三类传感器从“任意单机命中即发现位置的 Radar/ESM”改为始终开启的被动信号传感器：单机只有方位，两机同时同源有效探测后条件释放真实位置。
3. 保持现有信息新鲜度 `I` 与规划价值 `V` 的定义，同时让 AIS、无源方位、多机条件位置、识别和任务中断以可追溯证据影响价值场。
4. 消除候选 Top-K 在进入 LLM 之前造成的不可见性，并保留确定性几何可行性校验与 UAV 最小代价匹配。
5. 支持显式 UAV/船舶数量、I 类船舶/II 类船舶比例，以及初始化阶段的船舶放置和删除。
6. 保证所有船舶在生成、手动放置和运动过程中始终位于整体任务边界和可航水域内。
7. 给出可复现、可统计的发现、识别、持续观察、接力和响应时延验收口径。
8. 建立“观测/事件 -> 信息更新与衰减 -> 价值场版本 -> LLM 区域划分与 UAV 调度 -> 新观测”的可验收闭环。

不在本轮范围内：遇险救援、AIS 欺骗、模型训练、独立微服务拆分、LLM verify 评分、无条件把仿真真值暴露给蓝方算法。唯一例外是 D08：多机有效探测门成立后，环境把该采样时刻的辐射源真实位置作为 `PassivePosition` 条件释放。

## 2. 当前实现基线与差距

| 领域 | 当前实现 | 本轮差距 |
| --- | --- | --- |
| UAV 数量 | `uav.count_max` 驱动实体和 `StateManager` | 字段语义仍像“上限”；需改为实际数量并验证 >=10 正式场景 |
| 船舶数量 | `initial_ship_count` + `target_ship_count` | 需改为总数 + 两类比例，统一“II 类船舶”术语 |
| 船舶运动 | 为每船规划通向边界出口的路线，越界后 `departed` | 与“不得越出整体任务边界”冲突 |
| AIS | 全局接收，广播内容不含真值 | 缺少每个 AIS 源独立的价值更新资格 `status` |
| 传感器 | SAR、EO/IR、可直接命中目标的 `RadarSensor` | 缺少辐射源、单机方位测量和多机位置条件释放门槛 |
| 信息价值 | `V=clip(alpha(1-I)+beta*S+gamma*A)`，marker 用点高斯 | 方向线索不能用点 marker 表达；缺少证据来源、有效期和撤销语义 |
| 更新触发 | `TriggerManager` 按事件名称分 heavy/light，信息场与候选各自读取 | 缺少观测到证据的中央规则、变更版本/脏区、数值门槛与紧急重规划语义 |
| 识别 | 接触历史 + 两阶段 EO + LLM 研判 | 类别与违规状态未拆开；仍使用 `target` 术语 |
| 接力 | 返航目标报告 + handoff candidate +统一调度 | 返航价值提升的空间含义和接力成功口径不明确 |
| LLM 范围 | 候选提取先截断，LLM 再选择任务 ID | 合法区域可能长期不进入模型输入 |
| 船舶编辑 | 仅有人工重点区编辑 | 缺少右侧组件库、放置/删除命令和仿真线程事务 |
| 指标 | 已有 outcome evaluator 与日志 | 尚无本轮 85%/2 秒的正式分母、计时点和无效场景规则 |

基线核验命令：

```bash
python -m pytest tests/schedule/test_info_field.py \
  tests/schedule/test_candidate_extractor.py \
  tests/schedule/test_output_validator.py \
  tests/mission/test_ship_population.py \
  tests/mission/test_ais_generation.py \
  tests/mission/test_contact_store.py \
  tests/mission/test_mission_scheduler.py \
  tests/env/test_server_runtime.py -q
```

2026-09-15 实测结果：`107 passed in 10.38s`。

## 3. 方案比较与推荐

### 3.1 LLM 区域规划权限

| 方案 | 做法 | 优点 | 风险 |
| --- | --- | --- | --- |
| A. 保持当前 Top-K | 只把最高分候选交给 LLM | 修改最少、输出最稳 | 不能消除候选外区域不可见问题 |
| B. LLM 自由输出矩形 | 输入完整价值网格，由 LLM 任意画框 | 自由度最高 | 几何失败率高，难保证覆盖、公平和 2 秒时延 |
| **C. 完整可行池 + 公平窗口（推荐）** | 确定性枚举覆盖完整的可行矩形池；每轮按高价值、最久等待、地理覆盖三类配额送入 LLM | 保留安全校验，可证明无饥饿，输入有界 | 需要候选缓存和公平队列 |

推荐 C。候选提取只负责“哪些矩形可执行”，不再用价值 Top-K 替 LLM 做最终策略裁剪。LLM 仍选择任务，不直接构造无法执行的几何，也不直接绑定具体 UAV ID。

### 3.2 无源位置释放规则

按用户确认采用简化仿真规则，不实现方位交会、WLS 或协方差估计：

- 同一采样时刻只有 1 架 UAV 有效探测到某辐射源：只发布含噪方位，不发布位置。
- 同一采样时刻至少 2 架不同 UAV 有效探测到同一辐射源：环境直接发布该辐射源在该采样时刻的真实位置。

真值位置只能在“同源、同时刻、至少两架都探测成功”的门成立后被条件释放；单机观测、不同采样时刻或不同辐射源的观测不得触发位置释放。

### 3.3 船舶边界行为

| 方案 | 做法 | 结论 |
| --- | --- | --- |
| 到边界停止 | 制动并保持 | 简单但产生大量不真实静止目标 |
| 瞬时镜面反射 | 直接翻转速度分量 | 位置安全，但违反转弯率/惯性 |
| **预测反射航向 + 动力学可行重规划（推荐）** | 提前预测越界，计算反射期望航向，再用现有转弯/加速度约束和 A* 生成内向航段；不可行时制动 | 同时满足边界与物理约束 |

### 3.4 人工编辑时机

推荐仅在首个仿真步之前编辑。这样删除船舶不会产生“历史证据是否抹除、已分配任务如何回滚、指标分母如何变化”的歧义。运行期编辑可作为后续独立需求，通过显式 scenario event 实现，不能复用初始化编辑语义。

## 4. 待审批设计决策

以下是对原需求及本轮闭环补充的建议默认方案。批准本文即表示接受这些决策；任何一项可单独修改。

| ID | 建议决策 |
| --- | --- |
| D01 | 船舶类别和违规状态拆为两个维度：`vessel_class=unknown/type_i/type_ii`，`activity=unknown/normal/suspected_violation/confirmed_violation`。首版违规活动定义见 7.1。 |
| D02 | 采用“完整可行候选池 + 公平 Prompt 窗口”，LLM 选任务 ID，确定性匹配器选 UAV ID。 |
| D03 | I 类船舶确认前的 AIS 价值影响不追溯撤销；确认后只禁止该 MMSI 产生新的原始 `ais_position` 价值证据，既有证据自然衰减。由 AIS 航迹独立检出的逃逸行为不受该过滤限制。 |
| D04 | 配置保留总数和两个比例；使用最大余数法转为整数，比例和必须为 1。手动增删只改变当前场景数量，不回写初始化配置。 |
| D05 | 船舶编辑只允许 `sim_time_min == 0` 且尚未执行首步；运行和回放均只读。 |
| D06 | 手动新船由服务端生成航向、速度、AIS 和闭合巡逻路线；非法位置返回可机读错误；首次 AIS/传感器采样延后到编辑关闭后的第一步，因此初始化删除时还不存在待清理的任务/证据。 |
| D07 | 船舶使用预测反射航向和动力学可行重规划，不能越界，也不再以驶离边界结束生命周期。 |
| D08 | II 类船舶雷达辐射采用可复现的开/关更新过程；同一采样时刻只有 1 架 UAV 有效探测时只发布方位，至少 2 架不同 UAV 对同一辐射源都探测成功时直接发布该时刻真实位置，不做 WLS 或协方差估计。 |
| D09 | SAR 与 EO 是互斥的主动任务模式，切换耗时 0.5 仿真分钟；被动信号传感器独立且始终开启，切换期间也工作。 |
| D10 | 返航/中断价值更新中心是“最后有效目标估计向接力时刻的投影位置及其不确定区域”，不是 UAV 位置、基地或返航路径。 |
| D11 | 85% 指标按 11.2 的显式分母统计；2 秒从快照冻结计到合法 AssignmentBatch 形成，包含模型调用、校验和应用级重试。 |
| D12 | 开启 AIS 的船舶在有效跟踪 UAV 附近表现出持续远离、显著转向/加速且不是边界或障碍规避时，产生高价值 `evasive_maneuver` 证据并触发重规划；它不单独确认船舶类别或违法。 |
| D13 | 保留 `V=clip(alpha*(1-I)+beta*S+gamma*A,0,1)` 形式，将默认权重校准为 `0.45/0.35/0.20`（和为 1），避免未扫描区域默认饱和为 1；紧急证据立即 heavy trigger，其他信息变化达门槛或跨越候选阈值时触发。 |

## 5. 总体架构

```text
第 1 层：信息量更新触发机制
AIS / SAR / EO / passive bearing+position / 研判 / 逃逸 / 接力事件
       -> ObservationBus -> InformationUpdatePolicy -> EvidenceStore
                                                        |
第 2 层：信息量更新与衰减机制                 v
                         I 扫描刷新 + S/A 证据核 + 时间衰减
                                                        |
                                   InfoFieldDelta(version, dirty_bbox, cause)
                                                        |
第 3 层：LLM-based 区域划分与 UAV 调度              v
              TriggerManager -> 冻结 InformationSnapshot(version, I/S/A/V)
                    -> 完整可行候选池 -> 公平 Prompt 窗口
                    -> LLM 选 task ID -> 确定性校验/匹配 -> ControlCoordinator
                                                        |
                              SAR 搜索 / EO 观察 / 返航 / 接力
                                                        |
                                             新观测回到第 1 层
```

保持单仿真写线程：HTTP/WebSocket 线程只能把编辑命令放入有界幂等队列；`SimulationEngine` 在步进边界应用命令。环境真值、蓝方观测、规划意图和执行事实继续分层，不新增第二个仿真状态源。

三层之间只传递不可变快照。每批观测在一个仿真步边界内原子提交，信息场只增加一个 `version`；规划快照记录该版本，候选评分、Prompt、LLM 回复与 AssignmentBatch 必须引用同一版本。当前单写线程会在决策期间冻结仿真状态；未来若改为异步规划，提交时版本不匹配必须拒绝，不能把旧决策应用到新信息场。

## 6. 数据契约

新增类型放在 `src/mission/contracts.py`，均为 frozen dataclass；JSON 入口拒绝额外字段、bool 冒充数字、NaN/Infinity 和重复键。

```python
VesselClass = Literal["unknown", "type_i", "type_ii"]
ActivityState = Literal[
    "unknown", "normal", "suspected_violation", "confirmed_violation"
]
EvidenceKind = Literal[
    "ais_position", "sar_contact", "eo_class", "eo_activity",
    "passive_bearing", "passive_position", "evasive_maneuver",
    "type_ii_assessment", "violation_assessment", "handoff"
]

@dataclass(frozen=True)
class PointKernel:
    mean_cells: tuple[float, float]
    sigma_cells: float

@dataclass(frozen=True)
class BearingKernel:
    origin_cells: tuple[float, float]
    bearing_deg: float
    bearing_std_deg: float
    sigma_origin_cells: float
    range_decay_cells: float

@dataclass(frozen=True)
class CovarianceKernel:
    mean_cells: tuple[float, float]
    covariance_cells2: tuple[tuple[float, float], tuple[float, float]]

@dataclass(frozen=True)
class PassiveBearingObservation:
    observation_id: str
    sample_id: str
    emitter_track_id: str
    burst_id: str
    observed_at_min: float
    observer_uav_id: str
    observer_position_cells: tuple[float, float]
    bearing_deg: float
    bearing_std_deg: float

@dataclass(frozen=True)
class PassivePosition:
    position_id: str
    emitter_track_id: str
    burst_id: str
    sample_id: str
    observed_at_min: float
    position_cells: tuple[float, float]
    source_observation_ids: tuple[str, ...]

@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    kind: EvidenceKind
    source_id: str
    contact_id: str | None
    observed_at_min: float
    expires_at_min: float
    strength: float
    spatial: PointKernel | BearingKernel | CovarianceKernel

@dataclass(frozen=True)
class InfoFieldDelta:
    previous_version: int
    version: int
    changed_bbox: tuple[int, int, int, int]
    max_abs_value_delta: float
    value_changed: bool
    crossed_candidate_threshold: bool
    urgent: bool
    reason_codes: tuple[str, ...]
    cause_evidence_ids: tuple[str, ...]

@dataclass(frozen=True)
class InformationSnapshot:
    version: int
    frozen_at_min: float
    info: tuple[tuple[float, ...], ...]
    strategic: tuple[tuple[float, ...], ...]
    timeliness: tuple[tuple[float, ...], ...]
    value: tuple[tuple[float, ...], ...]
    recent_deltas: tuple[InfoFieldDelta, ...]

@dataclass(frozen=True)
class EvasiveManeuverFact:
    fact_id: str
    evasion_episode_id: str
    episode_started: bool
    mmsi: str
    contact_id: str
    observed_at_min: float
    position_cells: tuple[float, float]
    covariance_cells2: tuple[tuple[float, float], tuple[float, float]]

@dataclass(frozen=True)
class AisUpdateState:
    mmsi: str
    enabled: bool
    revision: int
    changed_at_min: float
    reason: Literal["unclassified", "confirmed_type_i"]

@dataclass(frozen=True)
class VesselCommand:
    command_id: str
    episode_id: str
    operation: Literal["create", "delete"]
    vessel_id: str | None
    expected_revision: int | None
    vessel_class: Literal["type_i", "type_ii"] | None
    position_cells: tuple[float, float] | None
```

`ShipTruth` 改为 `vessel_class` 和 `activity_schedule`，只允许 `src/env` 与 `OutcomeEvaluator` 读取。`ContactSnapshot` 使用算法估计的 `vessel_class` 与 `activity`；不再用一个 `target` 标签同时表达类别、违规和任务目标。

研判输出也保持双维证据和置信度，避免一个 confidence 同时修饰两个不同结论：

```python
@dataclass(frozen=True)
class ContactAssessment:
    vessel_class: VesselClass
    class_confidence: float
    class_evidence_ids: tuple[str, ...]
    activity: ActivityState
    activity_confidence: float
    activity_evidence_ids: tuple[str, ...]
```

`class_evidence_ids` 只能引用 EO class/SAR class 等类别证据；`activity_evidence_ids` 只能引用 `eo_activity/survey_motion/radiation_activity`。`eo_class` 不能充当第二个违规证据族。

`PassiveConfig` 的首版完整字段与建议值如下。多机位置释放使用全局固定采样时钟的同一 `sample_id`，因此不再配置融合时窗、交会角或定位质量门：

| 字段 | 建议值 | 校验 |
| --- | ---: | --- |
| `measurement_interval_min` | 1.0 | 有限且 >0 |
| `reference_detection_probability` | 0.90 | 有限且在 [0,1] |
| `detection_range_cells` | 10.0 | 有限且 >0；范围外 Pd 固定为 0 |
| `range_scale_cells` | 10.0 | 有限且 >0 |
| `bearing_std_deg` | 3.0 | 有限且在 (0,90) |
| `received_power_std_db` | 2.0 | 有限且 >=0 |
| `reference_distance_cells` | 1.0 | 有限且 >0 |
| `minimum_received_power_db` | -90.0 | 有限 |
| `position_association_radius_cells` | 1.0 | 有限且 >0 |

`EmitterConfig` 至少包含 `mean_silent_interval_min=10.0`、`burst_duration_min=[0.5,2.0]` 和有限的 `source_power_at_reference_db`，并验证正数、有序区间。

## 7. 具体算法

### 7.1 违规作业的可观测定义

首版建议把“违规作业”定义为II 类船舶在配置的监管区域内执行持续测线作业。环境生成隐藏活动阶段：`transit -> survey -> transit`。算法不得读取阶段，只能使用以下观测：

1. EO 作业特征：在有效 EO 观测下，以独立检测概率输出 `type_ii_equipment_score` 和 `deployed_equipment_score`，不输出真值类别。
2. 航迹特征：最近 20 分钟观测轨迹中，位于监管区的时长不少于 12 分钟，速度中位数低于或等于 `observed_speed_max_kn`，并出现至少 2 次大于或等于 `reversal_angle_min_deg` 的往返转向。
3. 辐射特征：多机条件释放的真实辐射源位置与同时刻接触位置的距离不大于 `position_association_radius_cells`，并在 10 分钟内至少出现 2 个独立 burst。

类别研判与活动研判分开：EO 船型/设备证据用于 `vessel_class`；测线航迹、部署设备和辐射组合用于 `activity`。建议 `confirmed_violation` 至少需要两个独立证据族，其中一个必须是 EO 作业特征或测线航迹，AIS 关闭不能参与违规确认。

这组阈值是建议配置，不是需求原文给定值；若 D01 不批准，实施时只完成类别识别和证据采集，`activity` 保持 `unknown`。

首版 `ActivityConfig` 建议值：`regulated_bboxes=[[8,8,22,22]]`（半开 bbox）、`schedule_start_min=[30,120]`、`schedule_duration_min=[60,120]`、`survey_command_speed_kn=10`、`survey_track_spacing_cells=1`、`trajectory_window_min=20`、`min_observed_duration_min=12`、`observed_speed_max_kn=12`、`reversal_angle_min_deg=120`、`min_reversal_count=2`、`radiation_window_min=10`、`min_distinct_bursts=2`。EO 特征建议 `type_ii_equipment_pd=0.90/pfa=0.05`、`deployed_equipment_pd=0.85/pfa=0.05`。所有 bbox 必须位于整体边界且含可航水域；概率在 [0,1]，时间/间距/速度为正，角度在 (0,180]，区间有序，`ship.speed_min_kn <= survey_command_speed_kn <= observed_speed_max_kn <= ship.speed_max_kn`。

II 类船舶的隐藏 schedule 由独立 RNG 生成，进入 survey 时在监管 bbox 内生成间距为 1 cell 的往返平行测线，并以 10 kn 指令速度执行；退出 survey 后恢复与I 类船舶同分布的正常巡逻。环境不得把 schedule/测线标签发布给算法，算法只能从含噪 EO 和接触轨迹重建 7.1 的特征。

`EvasionConfig` 的建议值为：`observer_range_cells=6`、`history_window_min=6`、`response_window_min=3`、`minimum_samples_per_window=3`、`minimum_window_span_min=1.5`、`minimum_course_change_deg=45`、`minimum_speed_increase_kn=3`、`minimum_outward_speed_kn=3`、`minimum_range_increase_cells=0.05`、`confirmation_samples=2`、`forced_maneuver_exclusion_cells=2`、`rearm_clear_min=5`、`evidence_ttl_min=20`、`evidence_tau_min=8`。所有时间/距离/速度为正，角度在 (0,180]，`response_window_min < history_window_min`，`minimum_samples_per_window>=2`，`minimum_window_span_min<=response_window_min`。

`InformationUpdateConfig` 的建议值为：`value_alpha=0.45`、`value_beta=0.35`、`value_gamma=0.20`、`material_delta_threshold=0.05`、`normal_heavy_cooldown_min=1`、`kernel_epsilon=0.01`。权重必须非负、有限且和为 1；其他阈值在 (0,1]。这些数值属于 D12/D13 的待审建议，不是原需求已给定参数。

### 7.2 船舶数量与比例取整

配置字段：

```yaml
population:
  total_count: 20
  type_i_ratio: 0.70
  type_ii_ratio: 0.30
```

严格要求有限数、`0 <= total_count <= cols*rows`、两个比例位于 `[0,1]`，且 `abs(sum-1) <= 1e-9`。先做 `normalized[k]=ratio[k]/sum(ratio)` 消除容差内的浮点偏差，再用 normalized 比例执行最大余数法；计算后必须断言 `0 <= remaining < 2`，否则配置错误。实际放置还要满足可航水域和最小间距，无法放置时显式初始化失败。整数分配为：

```text
quota[k] = total_count * normalized[k]
count[k] = floor(quota[k])
remaining = total_count - sum(count)
按 (quota-count) 降序分配 remaining；相同余数按 type_i、type_ii 固定顺序
```

由独立 RNG 子流打乱类别槽位，确保改变 AIS 或运动参数不会改变类别抽样。手动编辑维护 `configured_counts` 与 `actual_counts` 两组只读统计，禁止把当前数量写回 YAML。

### 7.3 II 类船舶随机辐射

每艘II 类船舶有独立 `EmitterState(off/on, next_transition_min, burst_id)`。关闭态到开启态的等待时间服从指数分布：

```text
wait = -mean_silent_interval_min * ln(1-u), u in (0,1)
duration = Uniform(burst_duration_min[0], burst_duration_min[1])
```

只在 step 跨过 `next_transition_min` 时转换，使用 `while` 消化一个大步内的多次转换，保证结果不依赖步长分割。I 类船舶首版不产生该项目定义的研究雷达 burst。辐射 RNG 按 `episode_seed + ship_id + emitter` 派生并写入 manifest，可完全复现。

建议初始参数：平均静默间隔 10 分钟、burst 持续 0.5-2 分钟；属于 D08 审批内容。

### 7.4 单机方位观测

被动传感器独立于 `sensor_mode` 执行，并维护绝对仿真时间上的 `next_sample_min`；采样时刻按 `passive.measurement_interval_min` 固定推进，不依赖主循环如何拆分 `dt`。首版不模拟匿名虚警，因为需求未要求其后续关联语义。每个采样时刻按固定顺序处理：确认落入辐射 burst，要求 `d<=detection_range_cells`，计算含噪接收功率并要求不低于 `minimum_received_power_db`，再抽样距离相关探测概率，最后生成含噪方位：

```text
d = ||emitter_position - uav_position||
P_detect_per_sample(d) = 0,                                      if d > detection_range_cells
P_detect_per_sample(d) = p0 * exp(-(d / range_scale_cells)^2),   otherwise
bearing_true = atan2(dy, dx)
bearing_measured = wrap(bearing_true + Normal(0, sigma_bearing_deg))
power = source_power_at_reference_db
        - 20*log10(max(d, d0) / d0)
        + Normal(0, sigma_power_db)
```

如果实现改为连续 hazard 而不是固定采样，则必须使用 `P(dt)=1-exp(-lambda(d)*dt)`，不得把单次概率直接用于任意长度 step。距离和接收功率只在环境内部用于有效探测门控；单机输出边界只含 UAV 自身位置、方位和方位误差，不含接收功率、距离或辐射源位置，避免从传播模型反推出测距线索。单条方位转换为从 UAV 向前的概率走廊，不创建点接触。

全项目坐标约定保持现状：`GridCoord=(col,row)`，连续位置为 `(x=col,y=row)`，NumPy 场也使用 `field[col,row]`，0 度方位沿 col 正方向、90 度沿 row 正方向。新增公式、测试、frame 与 Canvas 转换都必须显式遵守该约定，不能套用 NumPy 常见的 `[row,col]` 心智模型。

### 7.5 多机条件位置释放

所有被动传感器使用 7.4 的全局固定采样时钟。每个采样时刻生成稳定 `sample_id=episode_id:sample_index`，环境对有效观测按 `(sample_id, emitter_track_id, burst_id)` 分组，对 `observer_uav_id` 去重。

```text
unique_observers = set(observation.observer_uav_id for observation in group)

if len(unique_observers) == 1:
    publish PassiveBearingObservation only
elif len(unique_observers) >= 2:
    publish PassivePosition(
        position_cells=emitter_true_position_at_sample,
        source_observation_ids=one_observation_per_distinct_uav_in_stable_order,
    )
```

“有效探测”仍指单机独立通过辐射 burst 激活、`detection_range_cells` 硬范围、最小接收功率和距离相关 Pd 抽样。两条来自同一 UAV 的重复报告只计一架；不同 `sample_id`、不同 `emitter_track_id` 或不同 `burst_id` 绝不合并。触发条件成立后，允许环境从辐射源真值取该采样时刻坐标，这是用户指定的仿真简化规则；输出仍不包含船舶类别、活动真值或未触发时的位置。

`PassivePosition` 对同一 group 幂等，`position_id` 由 group key 派生。发布位置时仍保留所有单机方位记录用于审计，但信息场以精确位置证据为主：同 group 的方位走廊不再参与活动 `S/A` 聚合，避免同一观测重复增益。

### 7.6 证据到信息价值的映射

`I` 只由有效 SAR/EO 扫描更新，证据事件不直接改 `I`。扫描覆盖的 cell 记录 `last_scan_time` 和 `scan_kind=search/track`，并将 `I` 刷新为 1；其后按绝对时间计算，不逐步乘衰减因子：

```text
I(c,t) = 0,                                              if never scanned
I(c,t) = exp(-ln(2)*(t-last_scan_time(c))/half_life_k), otherwise
k = search or track
```

这保证将同一时间区间拆成不同 step 时结果一致。`V` 保留现有 alpha/beta/gamma 形式；本轮把现有点 marker 泛化为 `EvidenceRecord -> spatial kernel`，再聚合到 `S`、`A`：

```text
G_S(e,age) = max(0, 1-age/ttl_e)
G_A(e,age) = exp(-age/tau_e), age <= ttl_e
S(c) = max_e strength_e * K_e(c) * G_S(e,age)
A(c) = max_e strength_e * K_e(c) * G_A(e,age)
V(c) = clip(alpha*(1-I(c)) + beta*S(c) + gamma*A(c), 0, 1)
```

证据策略由 `EvidenceKind` 的中央只读表决定，记录本身只携带强度和已验证的空间参数，调用者不能任意选择衰减：

| EvidenceKind | 空间核 | 初始 strength | TTL（建议） | tau（建议） | 进入分量 |
| --- | --- | ---: | ---: | ---: | --- |
| `ais_position` | 点高斯 | 0.35 | 15 min | 5 min | S、A |
| `sar_contact` | 点高斯 | 0.55 | 30 min | 10 min | S、A |
| `eo_class` / `eo_activity` | 点高斯 | 观测 confidence | 60/30 min | 20/10 min | S、A |
| `passive_bearing` | 前向方向走廊 | 0.60 | 10 min | 3 min | S、A |
| `passive_position` | 真实位置点高斯 | 1.00 | 15 min | 5 min | S、A |
| `evasive_maneuver` | 船位协方差椭圆 | 1.00 | 20 min | 8 min | S、A |
| `type_ii_assessment` | 接触协方差椭圆 | 0.85 | 60 min | 20 min | S、A |
| `violation_assessment` | 接触协方差椭圆 | 1.00 | 60 min | 20 min | S、A |
| `handoff` | 投影协方差椭圆或方向走廊 | 1.00 | 接力 deadline | 5 min | S、A |

TTL/tau 是建议配置，属于 D03/D08/D10 的一部分。核函数按证据语义选择：

- AIS/EO/SAR 点位置：二维高斯核。
- PassivePosition：以条件释放的真实位置为中心使用 `marker_sigma_cells` 点高斯核。
- PassiveBearing：只对射线前方 `s>=0` 生效；横向标准差随距离扩张，`sigma_perp(s)=sigma_origin+s*tan(sigma_bearing)`，并乘 `exp(-s/range_decay)`。
- Handoff：以最后有效接触状态做匀速投影，`mu_h=x_last+v_hat*dt`；协方差按 `Sigma_h=Sigma_last+Q*dt^2` 扩张，使用椭圆高斯核。

每条证据有唯一 ID，重复输入幂等。过期表示不再参与 `S/A`，不表示目标消失。同一 `(subject_id, kind, episode_id)` 的持续观测使用 supersede：先关闭旧记录，再以新位置/方位、新时间和新强度原子写入，因此连续观测会移动并刷新高价值区，而不是无限叠加同一证据。确认I 类船舶时对其 MMSI 执行 `enabled=False`；后续 AIS 仍进入接触历史和逃逸特征检测，但不创建新的原始 `ais_position` 证据。既有记录不删除，按 D03 自然衰减。

D13 把默认权重从现有 `1.0/0.8/0.5` 校准为 `0.45/0.35/0.20`，但不改变公式。这使“未扫描且无证据”的 cell 价值为 0.45，刚扫描且无证据为 0，刚扫描且有满强度高价值证据为 0.55，未扫描且有满强度证据为 1。因此逃逸、多机条件释放的真实位置、违法研判会真正改变 `V` 数值，而不只是在饱和的 1 下增加隐藏分量。

#### 7.6.1 观测/事件到信息更新的中央规则

`InformationUpdatePolicy` 是唯一允许把观测或事件转为 `I` 更新和 `EvidenceRecord` 的组件；传感器、研判器和调度器不得直接改数组。首版规则表为：

| 输入事实 | 有效条件 | 信息操作 | 是否紧急 heavy trigger |
| --- | --- | --- | --- |
| SAR 覆盖 footprint | 扫描完成且 footprint 合法 | footprint 内 `I=1`；检出船时另写 `sar_contact` | 否，按变化门槛 |
| EO 有效 lock | 非 switching，观测质量通过 | footprint/track cell 的 `I=1`，写 EO 证据 | 研判变为 type_ii/violation 时是 |
| AIS 报文 | 报文合法且 registry enabled | 更新接触，supersede `ais_position` | 否，按变化门槛 |
| AIS 航迹逃逸 | 通过 7.6.2 状态机 | supersede `evasive_maneuver`，strength=1 | 仅新 episode 是 |
| 单机被动方位 | 接收功率/Pd 通过 | 写方向走廊，不写点位 | 否，按变化门槛 |
| 多机被动位置 | 同 sample/source/burst 下至少 2 架不同 UAV 都有效探测 | 写真实位置点核，创建/刷新 investigation task | 仅新 position track 是 |
| 类别/活动研判变更 | 修订号、证据引用均有效 | 写 `type_ii_assessment` 或 `violation_assessment` | 是 |
| 返航/观察中断 | handoff attempt 成立 | 写投影椭圆/方向走廊 | 是 |
| 时间流逝 | 每仿真步 | 按绝对时间重算 `I/S/A`，过期证据移出活动集 | 只由周期规划或阈值跨越触发 |

一批输入先全部验证，再在副本上应用；任一记录失败时整批不提交。dirty cells 是本步扫描 footprint、supersede 的旧/新核 support、新增/过期证据的 `K>=kernel_epsilon` support，以及自上个发布时刻起 `I/S/A` 因衰减发生变化的活动 cells 的并集；取最小半开 bbox 后重算并与提交前 `V` 比较，生成一个 `InfoFieldDelta`。任何已提交的 I/证据语义变化都把 `version` 增加一次并发布 delta；若新紧急证据被现有 max 聚合完全遮蔽，则 `value_changed=False`、`max_abs_value_delta=0`，但 `urgent=True`、cause ID 和新 version 仍保留。只有语义与数值都无变化的幂等重放才不增版本。

#### 7.6.2 开 AIS 船舶逃逸检测

检测只使用接收到的 AIS 航迹和 UAV 自身状态，不读取 `ShipTruth`。对每个 MMSI 维护 `clear -> candidate -> confirmed -> cooldown` 状态机；仅当有一架正在对该 contact 执行 probe/track 的 UAV，且船-UAV 距离不超过 6 cells 时评估。

对最近 6 分钟 AIS 航迹按半开时窗 `[t-6,t-3)` 和闭时窗 `[t-3,t]` 分组；每个窗口必须至少有 3 个不同时刻的有效 AIS 点，且最早与最晚点的时间跨度至少 1.5 分钟，否则本次返回 `insufficient_window_support`。对两个窗口分别做加权线性拟合得到船速向量 `v_pre/v_post`。在两个连续 AIS 评估时刻同时满足以下条件才确认一次逃逸 episode：

```text
angle(v_pre, v_post) >= 45 deg OR speed(v_post)-speed(v_pre) >= 3 kn
dot(v_post, unit(ship_position-uav_position)) >= 3 kn
distance(ship,uav)_now - distance(ship,uav)_3min_ago >= 0.05 cell
```

若船位距任务边界或已知障碍小于 2 cells，该次不判逃逸，避免把被迫规避当作行为证据。确认时产生唯一 `evasion_episode_id`，用当前 AIS 位置和航迹拟合协方差创建 strength=1 的椭圆证据。条件连续 5 分钟不成立才 rearm；同一 episode 只刷新证据，不重复触发 heavy 规划。

该证据的语义是“需高优先级复查的可疑行为”，不是“已确认违法”。`AisUpdateRegistry.enabled=False` 只抑制原始 AIS 位置对价值的更新，不抑制从航迹派生的逃逸事件；这使已判为I 类船舶的对象出现新异常时仍能被重新关注。

#### 7.6.3 信息场变化到规划触发

`TriggerManager` 消费 `InfoFieldDelta`，按以下优先级合并同一仿真步的变更：

1. 新 `evasive_maneuver` episode、新 passive-position track、新 type_ii/violation assessment 变更、新 handoff attempt 是紧急变化，在当前步边界产生一次 heavy trigger，不受普通 1 分钟 cooldown 限制；同一 episode/track/attempt 的后续 supersede 设 `urgent=False`，再按数值门槛处理。passive-position track 在连续 `2*measurement_interval_min` 无有效多机位置后结束，下一位置重新紧急触发。
2. 非紧急变化在 `max_abs_value_delta>=0.05` 或任一 cell 跨越 `candidate_value_threshold` 时请求 heavy trigger，普通 heavy 之间至少间隔 1 仿真分钟；期间的 delta 继续合并，不丢弃 cause ID。
3. 仅衰减且未跨阈值的变化不逐步调模型，由现有周期 heavy cycle 处理。资源到位、搜索完成等只需重配对的事件仍走 light trigger。
4. 同一步同时有多个原因时只冻结一个最新快照，`reason_codes` 保留全部原因，不启动并发 LLM 调用。

### 7.7 类别确认与 AIS 更新资格

`ContactStore.apply_assessment()` 成功提交 `vessel_class=type_i` 后，在同一仿真事务中：

1. 更新接触 revision 和类别。
2. 若有 MMSI，则以期望 revision 调用 `AisUpdateRegistry.disable(mmsi, reason)`。
3. 释放 probe/track 任务，但不删除接触、AIS 或历史证据。
4. 发出 `type_i_ais_value_disabled` 和 `type_i_confirmed` 事实事件。

事务失败时两者都不提交，避免类别已变但 AIS 仍更新价值。接触合并时，禁用状态按 MMSI 保存，不按可变化的 contact ID 保存。

`PassivePosition` 按 `emitter_track_id` 创建或刷新一条 signal contact，位置直接等于条件释放坐标。与已有 EO/SAR/AIS contact 合并时，把对方按现有航迹速度投影到 `PassivePosition.observed_at_min`，欧氏距离不大于 `position_association_radius_cells=1.0` 时可合并。半径内有多个 contact 时只有唯一最近者可合并；最近距离并列时以 `ambiguous_contact_association` 保留独立 signal contact，不用真值船舶 ID 消除歧义。

每个合并后 contact 维护最近 10 分钟不同 `burst_id`。只有至少 2 个独立 burst 都产生过 `PassivePosition`、且均与该 contact 合并，才发布 `radiation_activity` evidence family；单个 burst 不足以参与 D §7.1 的违规确认。

### 7.8 SAR、EO 与被动传感器状态机

```text
active_mode: standby <-> switching_to_sar <-> sar
             standby <-> switching_to_eo  <-> eo
passive_enabled: 恒为 True（实体存活期间不可由任务切换修改）
```

搜索任务申请 SAR，probe/track 申请 EO；切换 0.5 分钟内 SAR/EO 均不产生有效样本。返航和待机主动载荷进入 standby，被动接收继续。`SensorSnapshot` 同时发布 `active_mode`、`transition_remaining_min` 和 `passive_enabled`，禁止继续用单个 `sensor_mode` 暗示三类传感器一起开关。

### 7.9 船舶边界与巡逻路线

初始化和手动放置均要求连续位置在 `[margin, cols-margin) x [margin, rows-margin)`，所在 cell 为水域，且与岛屿/其他船满足间距。每艘船的 normal route 改为任务区内的闭合巡逻航路，不再选边界出口。

每个动力学子步先预测 `p_next`。若船体安全圆将在 lookahead 内穿越边界：

```text
v_reflected = v - 2 * dot(v, n) * n
heading_goal = atan2(v_reflected.y, v_reflected.x)
```

角点同时反射两个越界分量。随后复用 `MotionDynamics`、转弯半径和 `ShipNavigator._repair` 生成满足转弯率/加速度/岛屿净空的内向轨迹。若不能在剩余距离内完成转向，则生成已验证的制动路线；如果制动也不可行，本步保持在最后合法姿态并记录 `vessel_boundary_blocked`，不能 clip 到边界后继续运动。所有实际子步线段都做连续碰撞和边界检查。

### 7.10 接力状态机与空间语义

```text
observing --fuel/range/safety--> handoff_required --successor committed--> handoff_pending
handoff_pending --successor valid EO lock--> observing
handoff_required/pending --deadline--> attempt_failed --next trigger/resources--> handoff_required（新 attempt）
```

每次尝试使用不可变记录：

```python
@dataclass(frozen=True)
class HandoffAttempt:
    handoff_id: str
    contact_id: str
    source_uav_id: str
    successor_uav_id: str | None
    evidence_id: str
    required_at_min: float
    assignment_deadline_min: float
    lock_deadline_min: float
    assignment_committed_at_min: float | None
    eo_lock_acquired_at_min: float | None
    state: Literal["required", "pending", "succeeded", "failed"]
    failure_reason: str | None
```

默认 `assignment_deadline=required+5`、`lock_deadline=required+10`。超时把本 attempt 终结为 failed，但 contact task 仍未完成；只要仍需观察，下一调度触发可创建新 attempt。指标分母按最初中断事件计一次，后续 attempt 是同一中断的重试，不能重复扩充分母。

触发接力时冻结最后一条有效 EO/SAR/passive position 接触状态，按 7.6 投影为 handoff evidence；若只有方位，不得伪造投影点，只保留方向走廊并创建方向搜索任务。原 UAV 退出只完成自己的 sortie，不完成 contact task。调度器把 handoff task 设为高优先级，可抢占普通搜索，但仍需满足航程、返航余量、传感器和冲突约束。

### 7.11 完整候选池与公平 Prompt 窗口

候选构建分四步：

1. 按所有整数宽高枚举半开矩形，面积位于 `search_min_cells..search_max_cells`，长宽比不超过阈值。
2. 排除越界、陆地、障碍、活动任务占用、无转弯净空或 coverage route 不可行的矩形；合法 shape/origin 和 coverage route 几何结果按 `obstacle_version` 缓存。值总和、未扫描数和占用矩形检测分别使用 `V`、seen、occupied 的 summed-area table，使每个 bbox 的动态评分/拒绝为 O(1)。
3. 对每个当前高价值可搜索 cell，验证至少属于一个可行候选。未覆盖 cell 进入 `unschedulable_cells`，携带确定性原因，不得静默丢弃。
4. `TaskCatalog` 为候选 bbox 保存稳定 ID 和 `eligible_since_min`，候选消失后结束年龄，重新出现时开启新资格期。

任务区域不是只有普通搜索矩形。`TaskCatalog` 对证据空间语义做以下确定性几何转换，LLM 从转换后的可执行 task ID 中选择：

| 证据/需求 | 任务区域构造 | 默认 task kind |
| --- | --- | --- |
| 普通全局价值 | 完整枚举的可行矩形，`utility=0.5*mean(V)+0.3*max(V)+0.2*unseen_fraction` | `search` / SAR |
| 单机 `passive_bearing` | 取方向核 `K>=0.1` 的前向走廊，按可行搜索尺寸切分为有序矩形 | `direction_search` / SAR |
| `passive_position` | 以真实位置所在 cell 为中心四周扩 1 cell，再扩展到最小可执行面积 | `investigation` / EO |
| `evasive_maneuver` | 用最新 AIS 航迹投影到预计到达时刻，取投影协方差 95% bbox 并扩 1 cell | `probe` / EO |
| type_ii/violation 研判 | 使用最新接触投影椭圆；已有观察任务时幂等刷新而不新建 | `track` / EO |
| handoff | 按 7.10 的投影椭圆或方向走廊 | `handoff` / EO 或 SAR |

点/椭圆 bbox 若小于该 kind 的最小可执行面积，以中心对称扩展；边界附近先向内平移再检查。无法构造可行区域时写入 `unschedulable_cells/reason`，不把估计点瞬移到可行区。

每轮 Prompt 的搜索候选容量为 `K_search`，按以下配额取并去重：50% 当前效用最高，25% FIFO 公平队列，25% 按 5x5 地理桶轮转；handoff/probe/track 任务先于该容量保留。公平队列每轮从上次 cursor 后继续，入选后移到队尾；价值和地理配额已选中的任务也视为本轮获得服务。在候选集合稳定、共有 `M` 个候选且公平配额 `q_fair>0` 时，任一候选进入模型输入的最坏上界是 `ceil(M/q_fair)` 个 heavy cycle。动态新增候选从队尾加入，不得插到队首使旧候选饥饿。

本设计中“LLM 任务区域划分”的精确语义是：LLM 在本轮可见的可执行 task 中选择一组两两不重叠、不与活动空域冲突、数量不超过可用资源的区域，并给出关注顺序和 defer 理由；不要求每轮将全图强制分割成无缝镶嵌。Prompt 目标按“紧急接力/异常 -> 本轮覆盖价值 -> 最久未服务 -> 转场/切换代价”从高到低表达。区域几何由确定性层保证可执行，具体 UAV 由匹配器分配，但“本轮做哪些区域”仍由 LLM 决策。

Prompt 另含完整 30x30 价值场的 5x5 分块摘要（36 块，每块含 `mean/max V`、`mean/max S`、`mean/max A`、`unseen`、`oldest_scan_age` 和最多 3 个 `cause_kind`）、本轮 `InfoFieldDelta.reason_codes/changed_bbox` 以及 `unschedulable_cells` 摘要，使 LLM 了解候选窗口之外的全局态势与价值成因。每个候选记录 `information_version`、`total/mean/max_value`、`dominant_cause_kinds` 和 `urgent_contact_id`；逃逸、多机无源位置、违法研判生成的 probe/track/investigation task 先于普通搜索候选进入 Prompt，但仍由 LLM 决定是否抢占。LLM 输出已知 task ID、显式 defer reason 和允许抢占的 UAV ID；严格校验后，由现有精确最小代价匹配选择 UAV：

```text
cost(u,t) = transit_time
          + lambda_fuel * required_range / remaining_range
          + lambda_switch * sensor_switch_time
          + lambda_preempt * preemption_penalty
```

匹配器只在可行边上求最大任务数、再求最小总代价。LLM 负责关注策略，确定性层负责具体资源可执行性。

### 7.12 初始化船舶编辑

右侧组件库包含两个可拖拽且可点击的条目：“I 类船舶”“II 类船舶”。交互流程：

1. 拖拽到地图，或点击组件后点击地图，得到连续 grid 坐标。
2. 前端只做即时边界提示；服务端是最终校验者。
3. `POST /api/vessels` 将命令放入 `VesselCommandQueue`，返回 202；主线程应用后通过 `/api/vessel-commands/{id}` 返回 applied/rejected。
4. 创建成功后服务端派生该船独立 RNG、AIS 状态、速度/航向和闭合巡逻路线。
5. 选中已有船舶后点击删除图标，`DELETE /api/vessels/{id}` 使用 episode ID 和 expected revision 防止删错。

引擎构造阶段只生成场景，不再立即执行当前的 `_refresh_ais_signals(0.0)`；首次 AIS、辐射和传感器采样统一发生在编辑关闭后的第一步。这样编辑期删除不需要回滚已发布接触或信息贡献，且第一帧的场景实体数与首次观测源一致。

手动创建使用 `episode_seed + command_id + "manual-vessel"` 派生 RNG，不受命令处理批次影响。I 类船舶 AIS 固定开启，II 类船舶按与初始化相同的 `type_ii_ais_enabled_probability` 抽样；正常航速从同一个与类别无关的配置分布抽样。路线生成从放置点出发，在 inset 可航水域采样 3-5 个互异 waypoint，以现有曲率约束 A* 逐段连接并闭合回首段；初始航向取第一段切线。任一段不满足连续边界、岛屿净空或制动余量时整条路线作废并重试，达到 `manual_route_attempt_limit` 后以 `route_unavailable` 原子拒绝，不能只留下船舶实体。

编辑器通过独立的 `GET /api/scenario/vessels` 读取 `scenario_entity_id/revision/position/vessel_class`。该端点只在 `editing_allowed=true` 时可用，关闭编辑后返回 409；响应不进入 mission frame、蓝方 Prompt、ContactStore 或 episode blue log。这里的类别和位置是操作者正在编辑的场景定义，不是算法观测。前端关闭编辑时立即清空 editor snapshot，运行期地图只根据 contact/evidence 显示算法可见目标。

错误码至少包括：`editing_closed`、`outside_task_boundary`、`not_navigable_water`、`overlaps_vessel`、`route_unavailable`、`episode_conflict`、`revision_conflict`。错误不产生半成品船舶。运行期和 replay 隐藏放置入口并禁用删除。

## 8. 事件顺序与一致性

每个仿真步固定顺序：

```text
应用初始化编辑命令（仅 t=0）
-> 更新时间/障碍/船舶运动与辐射状态
-> 全局 AIS 接收
-> 每架 UAV 被动方位采样
-> 按 sample/source/burst 分组，至少 2 架不同 UAV 有效探测后释放该时刻真实位置
-> SAR/EO 主动观测与 ContactStore 更新
-> 类别/活动研判、AIS 逃逸状态机及 AIS 更新资格事务
-> InformationUpdatePolicy 把本步事实原子转为 I 刷新/EvidenceStore 更新
-> 按绝对时间衰减 I/S/A，计算 V，产生 InfoFieldDelta/version
-> TriggerManager 合并本步 delta，决定 none/light/heavy
-> heavy 时冻结 InformationSnapshot，构建接力/异常/普通任务目录与公平 Prompt 窗口
-> LLM 选择、严格校验、确定性匹配
-> ControlCoordinator 原子提交
-> 评价器只读真值记分
-> 发布 frame 和日志
```

同一步产生的观测可以影响该步调度，但执行事实只有 `ControlCoordinator` 成功提交后成立。LLM 输出、价值升高或命令排队均不表示任务完成。

## 9. 失败处理

| 失败 | 行为 |
| --- | --- |
| 单机方位不足 | 保存方向证据，不发布位置 |
| 多机位置释放条件不足 | 保留各自单机方位；不发布位置，不跨 sample/source/burst 合并 |
| AIS 已禁用价值更新 | 接收并写接触历史；跳过新 `ais_position`，但仍允许航迹逃逸检测；记录 skipped reason |
| 信息更新批次含非法记录 | 整批不提交，版本不变；记录精确 evidence ID 和原因 |
| 候选 cell 不可调度 | 显式发布原因；不得伪造候选或静默删除 |
| LLM 超时/非法输出 | 调度器在 snapshot 冻结时建立配置化绝对 deadline；默认/fixture 使用 2.0 秒并为校验/匹配预留 0.2 秒，生产真实 LongCat 路由使用 30.0 秒并预留 0.5 秒（实测完整响应约 6–20 秒）。网关最多首次请求 + 2 次纠错且共用该 deadline，transport timeout 不得侵占后处理预留；任一阶段超时都不形成新 AssignmentBatch，保持已提交任务，发出 `decision_failed`，1 仿真分钟后重触发；不生成规则替代决策 |
| 接力无可用 UAV | 任务保持 handoff_required，证据继续衰减/扩张，下一触发周期重试 |
| 船舶边界无安全路线 | 制动或保持最后合法姿态，发出故障事件；绝不越界 |
| 编辑命令失败 | 原场景不变，命令返回稳定错误码 |
| 指标场景无分母 | 标记 N/A，不把 N/A 当 100% |

## 10. 可视化与可观察性

Frame schema 升级且保留显式版本。运行期地图只展示算法可见内容：单机方位为有角度误差宽度的射线扇区；至少两机同时同源探测后在地图上显示精确位置点；AIS 更新资格在接触面板显示“参与/不参与价值更新”；类别和违规状态分两行显示。初始化编辑器使用 7.12 的隔离 scenario snapshot，不复用 mission frame。隐藏真值船舶类别只允许在离线 evaluation artifact 中出现，不提供可在 live 蓝方界面误开的 overlay，也不写入蓝方 Prompt。

每次 heavy cycle 记录：`information_version`、触发 reason/evidence ID、变化 bbox 与 max delta、完整候选数、Prompt 候选 ID、各配额来源、被跳过次数、不可调度 cell、LLM 用时、校验用时、匹配用时和总用时。每条信息证据记录 source、geometry kind、created/superseded/expires、是否参与本轮 `S/A`，便于解某一区域为何高价值。UI 调试面板允许点选 cell 查看 `I/S/A/V` 和活动 cause，但不显示环境真值。

## 11. 验收指标建议

### 11.1 测试层次

1. 单元测试：比例取整、辐射时序、方位噪声、同 sample/source/burst 的多机真实位置释放、核函数、AIS 状态、边界反射、公平窗口。
2. 契约测试：真值隔离、严格 JSON、命令幂等、信息版本与脏区、frame schema、错误码。
3. 集成测试：AIS 确认前后、AIS 逃逸到高价值任务、单机方位到多机真实位置释放和调度、SAR/EO/被动并行、返航接力、候选公平性。
4. 属性测试：所有随机 seed/步长下船舶不越界；不同 sample/source/burst 组合不产生位置；配置数量与实体/UI 数量一致。
5. 场景验收：固定公开回归 seeds + 不参与调参的独立 holdout seeds；真实 LLM 运行与 fixture 测试分开报告。当前仓库中的 holdout seed 不是保密数据，文档不声称其隐藏。

三层闭环必须有两条完整验收 trace，不能只分别测每层：

```text
AIS-on 逃逸航迹 -> evasive_maneuver evidence -> V 局部数值上升
-> urgent heavy trigger -> 新 version 候选/Prompt -> LLM 选中复查任务
-> 确定性 UAV 匹配 -> ControlCoordinator commit

2 架 UAV 同 sample/source/burst 有效探测 -> 释放该时刻真实位置 -> passive_position evidence
-> V 点核区数值上升 -> investigation task -> LLM 选择
-> 可行 UAV 匹配 -> 后续 EO 观测 -> 新证据再次更新信息场
```

每条 trace 断言 `observation_id/evidence_id/info_version/task_id/decision_id/assignment_id` 六段关联完整，且 Prompt 的 `information_version` 等于候选评分和 AssignmentBatch 的版本。

### 11.2 指标定义

建议固定如下口径，待 D11 审批：

| 指标 | 分母 | 成功条件 |
| --- | --- | --- |
| 发现与跟踪任务完成率 | 在首步前场景中存在且 episode 有效运行至少 30 分钟的全部II 类船舶 | episode 结束前形成关联接触，并获得连续 5 分钟有效 EO 观察；未发现、错配、超时均失败 |
| I 类船舶/II 类船舶区分率 | 在首步前场景中存在且 episode 有效运行至少 30 分钟的全部船舶 | 使用 balanced accuracy=`(type_i recall + type_ii recall)/2`；证据不足而保持 unknown 或误判均计错，不以“获得充分证据”筛分母 |
| 接力成功率 | 因燃油、航程或安全原因中断且当时存在至少一架可行后继 UAV 的观察事件 | 后继 UAV 在 5 分钟内提交接力任务并在 10 分钟内恢复有效 EO lock；同一中断只计一次 |
| 持续观察率 | 评价器按隐藏真值统计的全部II 类船舶 `survey` 违规活动分钟并集；该真值只用于离线记分，不回灌算法 | 违规活动分钟中至少一架 UAV 有有效 EO lock 的分钟并集 / 全部违规活动分钟并集；换机重叠不重复计时；未发现或仍 unknown 的船仍贡献完整分母 |
| 在线规划响应 | 每次 heavy/light 触发，包括超时和非法输出 | `decision_finished_wall - snapshot_frozen_wall`，结束点是合法 AssignmentBatch 形成或本轮明确失败返回；包含 API、校验和应用级重试；fixture/本地网关非故障样本要求 <=2.0s，真实 LongCat 按生产 30.0 秒 deadline 分别报告 p50/p95/max |

85% 阈值分别用于前三项和持续观察率，不用多个指标相乘生成一个模糊总分。配置/模型故障、场景初始化失败单列 operational failure，不从分母中悄悄删除。真实 LLM 与固定 fixture 的结果分别标注。

## 12. 兼容与迁移

这是一次显式 schema 迁移，不长期维护双语义字段：

- `uav.count_max` -> `uav.count`，旧字段给明确迁移错误。
- `ship.initial_ship_count/target_ship_count` -> `ship.population.total_count/type_i_ratio/type_ii_ratio`。
- `vessel_class=target` -> `vessel_class=type_ii`；蓝方 `vessel_class` 同步迁移。
- `sensor.radar` -> `sensor.passive`；删除“任一传感器命中即发现”的融合规则。
- `departed` 不再作为正常船舶边界生命周期；仅保留在旧 replay schema 适配器中。

旧 replay 通过只读 adapter 映射为新 frame，不允许旧命名重新进入新运行时。README、系统参数和验证文档与代码在最终任务统一更新。

## 13. 审阅清单

请重点确认 D01-D13，尤其是：

1. 是否接受 7.1 的违规作业可观测定义与建议阈值。
2. 是否接受完整候选池 + 公平窗口，而不是 LLM 自由画框。
3. 是否接受I 类船舶确认前 AIS 影响不追溯撤销。
4. 是否接受仅初始化阶段编辑船舶。
5. 是否接受边界预测反射、SAR/EO 互斥 0.5 分钟切换，以及“单机只有方向、两机同时同源探测直接得到真实位置”的简化规则。
6. 是否接受 11.2 的指标分母、成功条件和 2 秒计时边界。
7. 是否接受 7.6.2 的 AIS 逃逸定义，以及“高价值复查线索但不单独定性违法”的语义。
8. 是否接受 `alpha/beta/gamma=0.45/0.35/0.20`、`0.05` 材料变化门槛和紧急事件立即 heavy trigger。

用户批准前，实施计划中的任务均保持未执行。
