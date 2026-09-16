# Maritime Requirements Alignment Implementation Plan

> 术语已按 2026-09-16 统一：分类使用 I 类船舶/II 类船舶，运行时值使用 `type_i`/`type_ii`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> 当前状态：待用户审阅，禁止开始代码实施。设计唯一来源为 `docs/superpowers/specs/2026-09-15-maritime-requirements-alignment-design.md`（下称 D）。

**Goal:** 在现有混合海上仿真上实现I 类船舶/II 类船舶双维识别、始终开启的单机被动方位与多机真实位置条件释放、可追溯信息价值、公平全局规划、边界内运动、初始化船舶编辑和明确验收指标，并打通“观测触发 -> 信息更新/衰减 -> LLM 区域划分与 UAV 调度 -> 新观测”的三层闭环。

**Architecture:** 保持单仿真写线程和现有 `ContactStore -> TaskCatalog -> MissionScheduler -> ControlCoordinator` 主链。第 1 层由 `InformationUpdatePolicy` 把 AIS/SAR/EO/被动/研判/接力事实转成标准更新；第 2 层由 `EvidenceStore + InfoField` 原子生成带版本的 `InfoFieldDelta`；第 3 层依该版本构建完整可行池和有界公平 Prompt。LLM 选任务，确定性算法校验几何并匹配 UAV，执行观测再回到第 1 层。

**Tech Stack:** Python 3、dataclasses、numpy、现有 scipy 可选路径、PyYAML、FastAPI/Pydantic、React/Canvas、pytest、Playwright。不得新增数据库、消息队列、粒子滤波或 agent 框架。

## Global Constraints

- 用户批准 D01-D13 前不得执行本计划，也不得修改业务代码。
- 场景真值坐标只能由 `src/env` 和 `OutcomeEvaluator` 主动读取；唯一的蓝方释放例外是 D08 的 `PassivePosition`，且必须先满足同 sample/source/burst 下至少两架不同 UAV 有效探测。除此之外的隐藏类别、活动、位置和物理绑定不得进入蓝方 snapshot、Prompt、frame 默认层或日志。
- AIS 不包含身份/位置欺骗；AIS 开关和 AIS 价值更新资格是两个独立状态。
- 单机被动观测只能发布方位；只有满足 D §7.5 全部门槛的多机观测才能发布位置。
- 保留 `V=clip(alpha*(1-I)+beta*S+gamma*A,0,1)` 形式；按 D13 将默认权重校准为 `0.45/0.35/0.20`，不另建冲突的“信息量”。
- 观测生成器不得直接写 `InfoField`；所有价值变化必须经 `InformationUpdatePolicy -> EvidenceStore/InfoField -> InfoFieldDelta`。
- SAR/EO 主动模式互斥，默认切换 0.5 仿真分钟；被动传感器始终开启。
- LLM 不直接绑定具体 UAV；所有执行任务经严格校验、确定性匹配和 `ControlCoordinator` 原子提交。
- 所有船舶的每个实际动力学子步必须位于整体任务边界和可航水域内。
- UAV 正式验收配置不少于 10 架；实现不得写死 10。
- 每个任务先写失败测试、确认失败原因、做最小实现、运行任务测试和回归测试，再提交独立 commit。
- 每个 task 的提交步骤前必须额外运行 `python -m pytest -q`；涉及前端的 task 还必须运行 build 和完整 Playwright。中间 commit 不能依赖“后续任务会修复”而处于不可运行状态。

---

## 0. 文件地图与执行门

新增文件：

| 文件 | 唯一职责 |
| --- | --- |
| `src/sensor/passive.py` | 被动探测概率、单机方位测量和多机真实位置条件释放 |
| `src/env/emitter.py` | II 类船舶辐射开/关过程，只属于环境真值 |
| `src/mission/evidence_store.py` | 证据幂等存储、过期、AIS 更新资格和空间核 |
| `src/mission/information_update.py` | 观测/事件到 I/证据的中央规则，批事务和 `InfoFieldDelta` |
| `src/mission/evasion_detector.py` | 基于 AIS 航迹与 UAV 自身状态的逃逸状态机 |
| `src/mission/prompt_window.py` | 候选配额、FIFO 公平轮转、等待上界和全局分块摘要 |
| `src/mission/vessel_commands.py` | 初始化船舶编辑命令、队列、结果和只读服务 |
| `tests/sensor/test_passive.py` | 单机方位与多机位置释放边界测试 |
| `tests/mission/test_evidence_store.py` | 证据/AIS 资格/核函数测试 |
| `tests/mission/test_information_update.py` | 触发规则、批原子性、版本、脏区和价值数值测试 |
| `tests/mission/test_evasion_detector.py` | 逃逸特征、强制规避排除、去重与 rearm 测试 |
| `tests/mission/test_prompt_window.py` | 公平窗口与无饥饿测试 |
| `tests/mission/test_vessel_commands.py` | 编辑命令契约和幂等测试 |
| `tests/env/test_emitter.py` | 辐射过程步长不变性测试 |
| `tests/env/test_vessel_boundary.py` | 船舶永不越界属性测试 |
| `tests/mission/test_maritime_acceptance.py` | 跨模块场景验收 |

主要修改文件：

| 文件 | 修改责任 |
| --- | --- |
| `configs/uav.yaml`、`configs/ship.yaml`、`configs/sensor.yaml`、`configs/mission.yaml`、`configs/environment.yaml` | 新配置 schema、信息权重和建议默认值 |
| `src/schedule/config_loader.py`、`src/mission/config.py` | 严格加载和跨字段验证 |
| `src/mission/contracts.py` | 新类型、双维识别、命令与 frame 契约 |
| `src/env/ship.py`、`src/env/ship_navigation.py` | 数量/比例、闭合巡逻、边界重规划 |
| `src/env/simulation.py` | 固定步序、被动链、证据事务、接力、编辑编排 |
| `src/schedule/info_field.py`、`src/schedule/state_manager.py` | 证据核聚合、信息版本/脏区和只读快照接口 |
| `src/schedule/candidate_extractor.py`、`src/mission/task_catalog.py` | 完整可行池、稳定资格年龄 |
| `src/schedule/task_allocator.py`、`src/mission/mission_scheduler.py` | 公平 Prompt 窗口和匹配代价 |
| `src/mission/contact_store.py`、`contact_assessor.py`、`trajectory_features.py` | 类别/活动拆分、AIS 禁用事务、违规证据 |
| `src/env/uav_entity.py`、`src/control/common/observation.py` | 主动载荷切换和 passive 恒开状态 |
| `src/vis/backend/server.py`、`frame_builder.py` | 船舶编辑 API、frame v3 |
| `src/vis/frontend/src/App.jsx`、`components/RightSidebar.jsx`、`components/CanvasMap.jsx`、`renderer/layers.js` | 组件库、放置/删除、被动方位/位置展示 |
| `src/mission/outcome_evaluator.py`、`episode_logger.py` | 指标分母、时延和可追溯日志 |

- [ ] 批准后记录基线：`git status --short`、`git rev-parse HEAD`、`python -m pytest --collect-only -q`、`npm --prefix src/vis/frontend run build`。
- [ ] 在独立分支/worktree 执行；不得提交现有未跟踪需求文档或其他用户文件，除非用户明确要求。
- [ ] 每个 task 的进度写入 `docs/implementation/2026-09-15-maritime-alignment-progress.md`，记录 commit、测试、实际模型调用与偏差。

依赖顺序：T1 -> T2 -> T3 -> T4 -> T5 -> T6 -> T7 -> T8；T9 依赖 T1/T6/T7/T8；T10 依赖 T2/T3/T6；T11 依赖 T5/T6/T7/T10；T12 依赖 T8/T9；T13 依赖全部任务。三层闭环的主实施映射是 `T4/T5/T7 -> T6 -> T9 -> T13`。该顺序也避免多个执行者同时修改 `SimulationEngine`、`contracts.py` 或 `task_catalog.py`。

三层实施与验收映射：

| 层 | 实施任务 | 主要产物 | 独立验收 | 闭环验收 |
| --- | --- | --- | --- | --- |
| 1. 信息更新触发 | T4 辐射、T5 方位/位置、T7 AIS 逃逸与研判、T8 接力，统一进 T6 policy | 带 observation/event ID 的规范 fact | emitter/passive/evasion/handoff 单元与集成测试 | T13 逃逸、passive-position trace 起点 |
| 2. 信息更新与衰减 | T1 参数契约 + T6 批事务、核、衰减、版本 | `EvidenceRecord`、`I/S/A/V`、`InfoFieldDelta`、`InformationSnapshot` | 数值变化、幂等、supersede、step invariance、dirty bbox 测试 | T13 断言局部 `V` 上升及新 version |
| 3. LLM 区域划分与 UAV 调度 | T9 触发、候选、Prompt、LLM、校验、匹配；T12 审计 | 同 version 的 task/decision/AssignmentBatch | 无饥饿、区域不冲突、2 秒 deadline、最大基数最小代价匹配 | T13 断言 LLM 选 task、UAV commit、执行观测回流 |

不以“三层各自测试通过”代替闭环验收；T13 必须验证六段 ID 和信息版本连续性。

## Task 1: 冻结术语、配置与公共契约

**Files:**
- Modify: `configs/uav.yaml`
- Modify: `configs/ship.yaml`
- Modify: `configs/sensor.yaml`
- Modify: `configs/mission.yaml`
- Modify: `configs/environment.yaml`
- Modify: `src/schedule/config_loader.py`
- Modify: `src/mission/config.py`
- Modify: `src/mission/contracts.py`
- Modify: `tests/test_runtime_configuration.py`
- Modify: `tests/mission/test_config.py`
- Modify: `tests/mission/test_contracts.py`

**Interfaces:**
- Produces: `UAVConfig.count:int`; `PopulationConfig`; `PassiveConfig`; `EmitterConfig`; `ActivityConfig`; `EvasionConfig`; `InformationUpdateConfig`; `VesselClass`; `ActivityState`; D §6 的 observation/evidence/delta/snapshot/command 类型。
- Migration: T1 先建立新字段作为唯一可配置来源，并提供只读派生兼容属性，使中间 commit 全套测试可运行；兼容属性不得保存第二份状态，并在 T2/T5/T7 逐域迁移消费者、T13 删除。最终读取旧 YAML 字段必须明确报迁移错误。

- [ ] **Step 1: 写失败配置测试**

```python
def test_population_counts_use_largest_remainder():
    assert allocate_population(3, {"type_i": 0.5, "type_ii": 0.5}) == {
        "type_i": 2, "type_ii": 1,
    }

@pytest.mark.parametrize("count", [True, -1, 9])
def test_formal_uav_count_rejects_invalid_values(count):
    with pytest.raises(ValueError):
        validate_uav_count(count, formal_acceptance=True)
```

- [ ] **Step 2: 运行测试确认因新接口缺失而失败**

Run: `python -m pytest tests/mission/test_config.py tests/mission/test_contracts.py tests/test_runtime_configuration.py -q`

Expected: FAIL，首个失败指向 `PopulationConfig`/`PassiveConfig`/新 vessel_class 字段不存在，而不是 YAML 语法或环境错误。

- [ ] **Step 3: 实现最大余数分配和严格配置**

```python
def allocate_population(total: int, ratios: dict[str, float]) -> dict[str, int]:
    order = ("type_i", "type_ii")
    ratio_sum = math.fsum(ratios[key] for key in order)
    normalized = {key: ratios[key] / ratio_sum for key in order}
    quotas = {key: total * normalized[key] for key in order}
    result = {key: math.floor(quotas[key]) for key in order}
    remaining = total - sum(result.values())
    if not 0 <= remaining < len(order):
        raise ValueError("population allocation is numerically inconsistent")
    ranked = sorted(order, key=lambda key: (-(quotas[key] - result[key]), order.index(key)))
    for key in ranked[:remaining]:
        result[key] += 1
    return result
```

加入跨字段检查：比例和容差 `1e-9`，分配前归一化且 `total_count<=cols*rows`；正式 profile `uav.count>=10`；切换时间非负；`PassiveConfig` 完整实现 D §6 的 9 个字段，`reference_detection_probability` 在 [0,1]、`bearing_std_deg` 在 (0,90)、`measurement_interval_min/detection_range_cells/range_scale_cells/reference_distance_cells/position_association_radius_cells` 为正、`received_power_std_db` 非负、`minimum_received_power_db` 有限；`ActivityConfig` 完整实现 D §7.1 的 bbox、schedule、测线、轨迹门槛、burst 和 EO Pd/Pfa 字段及交叉校验；`EvasionConfig` 实现 D §7.6.2 的 14 个字段，并验证 response/history 顺序、每窗口样本/时跨、角度和正数；`InformationUpdateConfig` 要求 `alpha/beta/gamma` 非负且和为 1，`material_delta_threshold/kernel_epsilon` 在 (0,1]；planning deadline 为 2.0 秒、postprocess reserve 为 0.2 秒且小于 deadline；burst 区间有序；禁止 bool 通过数值校验。

- [ ] **Step 4: 完成术语 schema 迁移**

增加 `vessel_class: unknown/type_i/type_ii` 和独立 `activity` 作为新字段。为中间 commit 提供从新字段计算的只读 `vessel_class` adapter，不允许 setter；保留旧 replay 的适配 Literal 只能放在 frame adapter。T7 迁移最后一批运行时消费者，T13 删除 adapter。

- [ ] **Step 5: 运行配置和契约测试**

Run: `python -m pytest tests/mission/test_config.py tests/mission/test_contracts.py tests/test_runtime_configuration.py -q`

Expected: PASS。

Run: `python -m pytest -q`

Expected: 全部现有测试 PASS，证明只读兼容层足以维持中间 commit 可运行。

- [ ] **Step 6: 提交**

```bash
git add configs/uav.yaml configs/ship.yaml configs/sensor.yaml configs/mission.yaml \
  configs/environment.yaml \
  src/schedule/config_loader.py src/mission/config.py src/mission/contracts.py \
  tests/test_runtime_configuration.py tests/mission/test_config.py tests/mission/test_contracts.py
git commit -m "refactor: align maritime terminology and configuration"
```

## Task 2: 按比例生成船舶并隔离类别/活动真值

**Files:**
- Modify: `src/env/ship.py`
- Modify: `src/env/ais_signal.py`
- Modify: `src/env/simulation.py:_create_ships`
- Modify: `tests/mission/test_ship_population.py`
- Modify: `tests/mission/test_ais_generation.py`
- Create: `tests/env/test_vessel_activity.py`

**Interfaces:**
- Consumes: `allocate_population()`、`PopulationConfig`。
- Produces: `create_ship_population(config, seed, navigable_mask, navigator) -> list[Ship]`；`ShipTruth(vessel_class, activity_schedule)`；公开 `Ship` 不用名称/类型泄漏类别。

- [ ] **Step 1: 写 N=0/1/3/20 和比例边界测试**

```python
@pytest.mark.parametrize("total,ratios,expected", [
    (0, (0.7, 0.3), (0, 0)),
    (1, (0.5, 0.5), (1, 0)),
    (3, (0.5, 0.5), (2, 1)),
    (20, (0.7, 0.3), (14, 6)),
])
def test_population_exact_counts(total, ratios, expected):
    ships = build_population(total, *ratios)
    assert len(ships) == total
    assert class_counts(ships) == expected
```

另测相同 seed 可复现、类别 RNG 改变不影响位置/路线/正常速度、I 类船舶 AIS 始终开启、II 类船舶 AIS 按独立 RNG 抽样、公开 AIS 字段相同分布且无 truth。

- [ ] **Step 2: 确认旧 target-count 实现导致测试失败**

Run: `python -m pytest tests/mission/test_ship_population.py tests/mission/test_ais_generation.py tests/env/test_vessel_activity.py -q`

Expected: FAIL，错误来自配置/字段仍使用 `target`。

- [ ] **Step 3: 实现类别槽位与活动日程独立 RNG**

```python
counts = allocate_population(cfg.total_count, {
    "type_i": cfg.type_i_ratio,
    "type_ii": cfg.type_ii_ratio,
})
slots = list(range(cfg.total_count))
random.Random(manifest["vessel_class"]).shuffle(slots)
type_ii_slots = frozenset(slots[:counts["type_ii"]])
```

为II 类船舶用独立 RNG 和 `ActivityConfig.schedule_start/duration` 创建隐藏 `activity_schedule`；I 类船舶 schedule 恒为空。算法快照不得序列化它。AIS 生成继续只读取 `ais_enabled` 和运动状态，不读取 `vessel_class`。

- [ ] **Step 4: 删除运行时 target 语义捷径**

更新 Contact/Ship 引用，不允许 `ship.vessel_class == "type_ii"` 直接创建蓝方已确认接触或任务。当前逃逸能力置于 `evasion.enabled`，默认 false；在 D01 之外不把转向、AIS 静默当违规。

- [ ] **Step 5: 运行船舶与真值隔离测试**

Run: `python -m pytest tests/mission/test_ship_population.py tests/mission/test_ais_generation.py tests/env/test_vessel_activity.py tests/mission/test_visibility.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/env/ship.py src/env/ais_signal.py src/env/simulation.py \
  tests/mission/test_ship_population.py tests/mission/test_ais_generation.py \
  tests/env/test_vessel_activity.py tests/mission/test_visibility.py
git commit -m "feat: generate type_i and type_ii vessel populations"
```

## Task 3: 船舶闭合巡逻与边界内动力学

**Files:**
- Modify: `src/env/ship.py`
- Modify: `src/env/ship_navigation.py`
- Create: `tests/env/test_vessel_boundary.py`
- Modify: `tests/mission/test_ship_navigation.py`
- Modify: `tests/env/test_simulation_integration.py`

**Interfaces:**
- Produces: `ShipNavigator.plan_patrol_route()`；`ShipNavigator.plan_survey_lawnmower(regulated_bbox, spacing_cells)`；`ShipNavigator.reflect_heading()`；`Ship.step()` 永不返回边界外 pose；正常生命周期不再产生 `departed`。

- [ ] **Step 1: 写边界公式和属性测试**

```python
def test_reflection_reverses_only_outward_component():
    reflected = reflect_velocity((2.0, 1.0), normal=(1.0, 0.0))
    assert reflected == pytest.approx((-2.0, 1.0))

@pytest.mark.parametrize("seed", range(50))
def test_every_executed_substep_stays_inside_task_boundary(seed):
    engine = build_engine(seed)
    for _ in range(480):
        engine.step()
        assert all(in_navigable_boundary(p) for ship in engine.ships for p in ship.last_actual_path)
```

- [ ] **Step 2: 运行并确认现有出口路线会失败**

Run: `python -m pytest tests/env/test_vessel_boundary.py tests/mission/test_ship_navigation.py -q`

Expected: FAIL，现有 `has_exited/departed` 行为被捕获。

- [ ] **Step 3: 实现预测反射与物理约束重规划**

```python
def reflect_velocity(v: Vec2, normal: Vec2) -> Vec2:
    projection = v[0] * normal[0] + v[1] * normal[1]
    return (v[0] - 2 * projection * normal[0],
            v[1] - 2 * projection * normal[1])
```

在 lookahead 安全边界触发内向 route；用 `MotionDynamics`、现有 `_repair()` 和 `braking_route()` 验证，不允许位置 clip 掩盖越界。闭合 patrol waypoint 全在 inset 水域，路线耗尽时选择下一个内部 waypoint。

II 类船舶进入隐藏 survey interval 时，在监管 bbox 内生成 1-cell 间距的往返平行测线并以 10 kn 指令速度执行；该值与现有 `ship.speed_min_kn=10` 一致。测线方向选择 bbox 长轴以减少转弯，端点先按转弯半径内缩并逐段通过现有连续安全检查。schedule 结束恢复普通 patrol。测试断言算法 snapshot/frame 不含 schedule 或 activity truth，只能观察到轨迹与含噪 EO 特征。

- [ ] **Step 4: 删除正常 departed 分支并保留旧 replay 适配**

更新 `SimulationEngine._update_ships()`、接触释放和指标逻辑。旧日志中的 departed 只在 replay adapter 显示，不能影响新 episode。

- [ ] **Step 5: 运行长时多 seed 回归**

Run: `python -m pytest tests/env/test_vessel_boundary.py tests/mission/test_ship_navigation.py tests/env/test_simulation_integration.py -q`

Expected: PASS；50 seeds x 480 steps 无越界，测试输出无 `departed` 正常事件。

- [ ] **Step 6: 提交**

```bash
git add src/env/ship.py src/env/ship_navigation.py src/env/simulation.py \
  tests/env/test_vessel_boundary.py tests/mission/test_ship_navigation.py \
  tests/env/test_simulation_integration.py
git commit -m "feat: keep vessels within the mission boundary"
```

## Task 4: 可复现雷达辐射过程

**Files:**
- Create: `src/env/emitter.py`
- Modify: `src/env/ship.py`
- Modify: `src/env/simulation.py`
- Create: `tests/env/test_emitter.py`
- Modify: `tests/mission/test_episode_logger.py`

**Interfaces:**
- Produces: `EmitterState`; `RadarEmitter.advance(end_min) -> tuple[EmissionInterval, ...]`；实例保存上次推进时间，只对II 类船舶实例化。

- [ ] **Step 1: 写转换、步长不变性和真值隔离测试**

```python
def test_emission_timeline_is_independent_of_step_partition():
    whole = simulate(seed=7, steps=[60.0])
    split = simulate(seed=7, steps=[1.0] * 60)
    assert whole == split

def test_type_i_has_no_type_ii_radar_emitter(type_i_ship):
    assert type_i_ship.radar_emitter is None
```

- [ ] **Step 2: 确认测试因 emitter 缺失而失败**

Run: `python -m pytest tests/env/test_emitter.py -q`

Expected: FAIL with import/interface missing。

- [ ] **Step 3: 实现连续时间更新过程**

```python
def exponential_wait(rng: random.Random, mean_min: float) -> float:
    u = max(rng.random(), sys.float_info.min)
    return -mean_min * math.log(u)

def advance(self, end_min: float) -> tuple[EmissionInterval, ...]:
    emitted = []
    while self.next_transition_min <= end_min:
        self._toggle_at(self.next_transition_min, emitted)
    return tuple(emitted)
```

确保大步跨越多个 transition 时不会漏事件；manifest 只记录 seed 派生信息，不把 source position 写进蓝方日志。

- [ ] **Step 4: 集成到船舶更新顺序**

`_update_ships()` 后更新 emitter interval，供本步被动采样读取；仅环境层知道 emitter 与 ship 的物理绑定。

- [ ] **Step 5: 运行测试并提交**

Run: `python -m pytest tests/env/test_emitter.py tests/mission/test_episode_logger.py -q`

Expected: PASS。

```bash
git add src/env/emitter.py src/env/ship.py src/env/simulation.py \
  tests/env/test_emitter.py tests/mission/test_episode_logger.py
git commit -m "feat: model reproducible type_ii radar emissions"
```

## Task 5: 单机方位与多机真实位置条件释放

**Files:**
- Create: `src/sensor/passive.py`
- Modify: `src/sensor/models.py`
- Modify: `src/env/uav_entity.py`
- Modify: `src/vis/backend/frame_builder.py`
- Create: `tests/sensor/test_passive.py`
- Modify: `tests/env/test_sensors_and_obstacles.py`
- Modify: `tests/env/test_frame_publisher.py`

**Interfaces:**
- Consumes: `PassiveConfig`、当前 emission intervals。
- Produces: `PassiveSensor.observe(sample_id, observer_uav_id, observer_position_cells, emitter_track_id, burst_id, emitter_position_cells, source_power_at_reference_db, observed_at_min) -> PassiveBearingObservation | None`；`PassivePositionResolver.release(observations, emitter_position_at_sample) -> PassivePosition | None`。resolver 只能从 `src/env/simulation.py` 的真值边界调用。

- [ ] **Step 1: 写单机/多机位置释放边界测试**

```python
def test_one_uav_publishes_bearing_but_never_position(resolver, one_bearing):
    position = resolver.release([one_bearing], emitter_position_at_sample=(5, 5))
    assert position is None

def test_two_uavs_same_sample_source_and_burst_release_true_position(resolver):
    observations = [bearing("U1", "S7", "E1", "B2"),
                    bearing("U2", "S7", "E1", "B2")]
    position = resolver.release(observations, emitter_position_at_sample=(5.25, 5.75))
    assert position.position_cells == (5.25, 5.75)
    assert position.source_observation_ids == tuple(
        item.observation_id for item in observations
    )
```

另测：同一 UAV 重复两条只计一架；不同 `sample_id`、不同 `emitter_track_id`、不同 `burst_id` 均不释放位置；范围外不产生观测；两架中任一架未通过 burst/硬范围/接收功率/Pd 时只保留已成功的单方位；3 架及以上时每架只选一条 source observation 并按 UAV ID 稳定排序；同 group 重放幂等。让 UAV 和船在采样期间持续运动，断言固定采样时钟和 RNG 在不同 engine step 分割下产生相同 `sample_id`、采样时间、观测者姿态和观测序列。

- [ ] **Step 2: 运行并确认新传感器接口缺失**

Run: `python -m pytest tests/sensor/test_passive.py -q`

Expected: FAIL with `src.sensor.passive` missing。

- [ ] **Step 3: 实现单机方位生成**

```python
delta = (emitter_position[0] - observer_position[0],
         emitter_position[1] - observer_position[1])
distance = math.hypot(*delta)
if distance > cfg.detection_range_cells:
    return None
true_deg = math.degrees(math.atan2(delta[1], delta[0])) % 360.0
measured_deg = (true_deg + rng.gauss(0.0, cfg.bearing_std_deg)) % 360.0
return PassiveBearingObservation(
    observation_id=observation_id,
    sample_id=sample_id,
    emitter_track_id=emitter_track_id,
    burst_id=burst_id,
    observed_at_min=observed_at_min,
    observer_uav_id=observer_uav_id,
    observer_position_cells=observer_position,
    bearing_deg=measured_deg,
    bearing_std_deg=cfg.bearing_std_deg,
)
```

按“burst active -> distance <= detection range -> received power >= threshold -> Bernoulli Pd -> bearing noise”的固定顺序生成观测；范围外直接返回 `None` 且不消耗 Pd/方位噪声 RNG，范围内功率衰减使用 `-20*log10(max(d,d0)/d0)`，使 source power 明确表示 d0 处功率。距离与接收功率只作环境内部门控，不写入 `PassiveBearingObservation`、frame、EvidenceRecord 或 Prompt；契约测试显式断言单机序列化结果没有 `received_power_db`、距离或辐射源位置字段。首版不生成匿名虚警。删除 `SensorSuite.detect()` 中 radar 任一命中即直接发现目标的路径。SAR 和 EO 保持各自职责。

- [ ] **Step 4: 实现多机条件真值释放**

先按 `(sample_id, emitter_track_id, burst_id)` 分组，每组按 `observer_uav_id` 去重。唯一 UAV 数小于 2 时返回 `None`；大于等于 2 时，由 `SimulationEngine` 在环境真值边界取该 emitter 在该 sample 时刻的真实坐标，构造 `PassivePosition`。`position_id` 由 group key 稳定派生，source observation 按 UAV ID 排序。resolver 不计算方位交点、WLS、协方差或交会角。

被动传感器用绝对 `next_sample_min` 固定采样时钟；同一个 `[start,end]` 区间无论被拆成多少个 engine step，都处理相同采样时刻和相同 RNG 次数。若以后改用 hazard，概率必须换算为 `1-exp(-lambda*dt)`。

- [ ] **Step 5: 发布只读 bearing/position frame 数据**

`frame_builder.py` 序列化 `PassiveBearingObservation` 和已通过多机条件门的 `PassivePosition`。frame 测试断言单机观测只有 observer position、bearing 与 bearing error，没有 position、range 或 received power；两机同 group 时 position 等于该 sample 真值，不同 sample/source/burst 不被合并。除了已条件释放的位置，frame 不读取其他 `ShipTruth`。

- [ ] **Step 6: 运行传感器测试和现有传感器回归**

Run: `python -m pytest tests/sensor/test_passive.py tests/env/test_sensors_and_obstacles.py tests/env/test_frame_publisher.py -q`

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add src/sensor/passive.py src/sensor/models.py src/env/uav_entity.py \
  src/vis/backend/frame_builder.py tests/sensor/test_passive.py \
  tests/env/test_sensors_and_obstacles.py tests/env/test_frame_publisher.py
git commit -m "feat: add passive bearings and multi-uav position release"
```

## Task 6: 中央信息更新策略、证据核与衰减

**Files:**
- Create: `src/mission/evidence_store.py`
- Create: `src/mission/information_update.py`
- Modify: `src/schedule/info_field.py`
- Modify: `src/schedule/state_manager.py`
- Modify: `src/mission/contact_store.py`
- Create: `tests/mission/test_evidence_store.py`
- Create: `tests/mission/test_information_update.py`
- Modify: `tests/schedule/test_info_field.py`
- Modify: `tests/mission/test_contact_store.py`

**Interfaces:**
- Consumes: 经验证的 AIS/SAR/EO/passive/assessment/handoff 事实批。
- Produces: `EvidenceStore.ingest/supersede/expire`；`AisUpdateRegistry.is_enabled/disable`；`InformationUpdatePolicy.apply_batch(facts, now) -> InfoFieldDelta | None`；`StateManager.freeze_information_snapshot() -> InformationSnapshot`。

- [ ] **Step 1: 写触发转换、价值数值和 AIS 状态失败测试**

```python
def test_bearing_evidence_raises_value_along_ray_not_behind_observer(field):
    field.ingest(bearing_evidence(observer=(10, 10), bearing_deg=0))
    value = field.get_value_matrix(1.0)
    assert value[15, 10] > value[10, 15]
    assert value[15, 10] > value[5, 10]

def test_confirmed_type_i_ais_is_received_but_no_longer_changes_value(system):
    before = system.ingest_ais(mmsi="1", at=1)
    system.confirm_type_i("C1", at=2)
    after = system.ingest_ais(mmsi="1", at=3)
    assert after.contact_updated
    assert not after.ais_position_evidence_created

def test_urgent_fact_changes_numeric_value_and_versions_once(system):
    system.apply_facts([sar_scan_fact(footprint=((12, 9),))], at=4.9)
    before = system.freeze_information_snapshot()
    delta = system.apply_facts([evasive_fact("M1", position=(12, 9))], at=5.0)
    after = system.freeze_information_snapshot()
    assert delta.urgent is True
    assert delta.version == before.version + 1 == after.version
    assert after.value[12][9] > before.value[12][9]

def test_invalid_fact_makes_whole_batch_atomic(system):
    before = system.freeze_information_snapshot()
    with pytest.raises(InvalidInformationFact):
        system.apply_facts([valid_sar_scan(), non_finite_evidence()], at=5.0)
    assert system.freeze_information_snapshot() == before

def test_new_urgent_fact_versions_even_when_max_aggregation_masks_value(system):
    system.apply_facts([full_strength_fact("OLD", position=(12, 9))], at=4.0)
    before = system.freeze_information_snapshot()
    delta = system.apply_facts([evasive_fact("NEW", position=(12, 9))], at=4.0)
    assert delta.version == before.version + 1
    assert delta.value_changed is False
    assert delta.max_abs_value_delta == 0.0
    assert delta.urgent is True
```

另测证据 ID 幂等、supersede 移动核而不叠加旧核、过期不等于接触消失、`passive_position` 精确点核、handoff 协方差扩张、确认前贡献不回滚、按 MMSI 而非 contact ID 禁用；验证默认 `alpha/beta/gamma=0.45/0.35/0.20`，未扫描无证据 cell 的 `V=0.45`，新鲜满强度证据必须对 `V` 产生可见数值变化。再覆盖扫描 footprint + 新旧 kernel support + 活动衰减 cells 的 dirty bbox、绝对时间衰减的 step-partition invariance、阈值跨越、幂等重放不增 version、新语义证据即使数值被 max 遮蔽也增 version、快照防御性拷贝。

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/mission/test_evidence_store.py tests/mission/test_information_update.py tests/schedule/test_info_field.py tests/mission/test_contact_store.py -q`

Expected: FAIL，现有 point marker/AIS 无资格状态，也没有中央更新策略与版本快照。

- [ ] **Step 3: 实现核函数和价值聚合**

```python
def bearing_kernel(cell, origin, bearing_rad, sigma_origin, sigma_angle, decay):
    delta = np.asarray(cell) - np.asarray(origin)
    direction = np.array([math.cos(bearing_rad), math.sin(bearing_rad)])
    along = float(delta @ direction)
    if along < 0:
        return 0.0
    perpendicular = abs(float(delta @ np.array([-direction[1], direction[0]])))
    sigma = sigma_origin + along * math.tan(sigma_angle)
    return math.exp(-0.5 * (perpendicular / sigma) ** 2) * math.exp(-along / decay)
```

保持 alpha/beta/gamma 公式并应用 D13 默认权重；marker compatibility wrapper 转为 point evidence，避免并存两套场。`passive_position` 使用以条件释放坐标为中心、强度固定为 `1.0` 的点高斯核；同一 group 已产生位置后，不再把该 group 的方位走廊计入活动 `S/A`，避免重复增益。

- [ ] **Step 4: 实现中央规则表、批原子性和变更快照**

`InformationUpdatePolicy` 对 D §7.6.1 的每种 fact 使用固定 mapper，生成 `ScanRefresh` 或由中央 policy table 决定 TTL/tau/kernel/strength 的 EvidenceDraft。调用者不得传入自定义 TTL/tau。所有 fact 先验证和展开，再在 `InfoField/EvidenceStore` 副本上应用；全部成功后单次 swap。使用扫描 footprint、supersede 新旧 support、新增/过期 kernel support 和活动衰减 cells 的并集求 dirty bbox；核 support 截断为 `kernel_epsilon=0.01`。比较新旧 `V`；只要 I/证据语义或数值变化就增加一次 version 并发布 `InfoFieldDelta`，数值被 max 遮蔽时明确设 `value_changed=False/max_abs_value_delta=0`。只有整批是无状态变化的幂等重放才返回 `None`。policy 对新 evasion episode 和新 passive-position track 设 `urgent=True`；同 episode/track 后续更新设 false。passive-position track 在 `2*measurement_interval_min` 没有新位置后关闭，之后再次满足多机条件时可重新紧急触发。

- [ ] **Step 5: 实现类别确认 + AIS disable 原子事务**

先构造新 contact 和 registry 副本，全部验证后一次提交；写两个事实事件。合并 contact 不改变 MMSI registry key。其后报文仍进入 AIS 航迹，但 mapper 跳过 `ais_position`；不得跳过后续 T7 的逃逸派生事件。

- [ ] **Step 6: 运行测试并提交**

Run: `python -m pytest tests/mission/test_evidence_store.py tests/mission/test_information_update.py tests/schedule/test_info_field.py tests/mission/test_contact_store.py -q`

Expected: PASS。

```bash
git add src/mission/evidence_store.py src/mission/information_update.py \
  src/schedule/info_field.py src/schedule/state_manager.py \
  src/mission/contact_store.py tests/mission/test_evidence_store.py \
  tests/mission/test_information_update.py tests/schedule/test_info_field.py \
  tests/mission/test_contact_store.py
git commit -m "feat: make information updates evidence driven and versioned"
```

## Task 7: AIS 逃逸检测、双维研判和传感器状态机

**Files:**
- Create: `src/mission/evasion_detector.py`
- Modify: `src/mission/trajectory_features.py`
- Modify: `src/mission/contact_assessor.py`
- Modify: `src/mission/contact_store.py`
- Modify: `src/mission/contracts.py`
- Modify: `src/mission/prompts/contact_assessor.txt`
- Modify: `src/env/eo_sensor.py`
- Modify: `src/env/uav_entity.py`
- Modify: `src/control/common/observation.py`
- Modify: `src/env/simulation.py`
- Modify: `tests/mission/test_trajectory_features.py`
- Create: `tests/mission/test_evasion_detector.py`
- Modify: `tests/mission/test_contact_assessor.py`
- Modify: `tests/mission/test_contact_store.py`
- Create: `tests/mission/test_sensor_modes.py`

**Interfaces:**
- Consumes: EO activity features、passive evidence、AIS track、UAV 自身位置/任务、`ContactSnapshot`。
- Produces: `ContactAssessment(vessel_class, class_confidence, class_evidence_ids, activity, activity_confidence, activity_evidence_ids)`；`EvasionDetector.evaluate(...) -> EvasiveManeuverFact | None`，fact 含 `evasion_episode_id/episode_started`；`ContactSnapshot.position_covariance_cells2`；`SensorSnapshot(active_mode, transition_remaining_min, passive_enabled=True)`。

- [ ] **Step 1: 写双维证据和模式切换测试**

```python
def test_type_ii_class_does_not_imply_violation(assessor):
    result = assessor.assess(type_ii_class_evidence_only())
    assert result.vessel_class == "type_ii"
    assert result.activity != "confirmed_violation"

def test_passive_remains_enabled_during_active_payload_switch(uav):
    uav.request_active_mode("eo")
    assert uav.active_mode == "switching_to_eo"
    assert uav.passive_enabled is True

def test_ais_track_turning_away_from_assigned_observer_confirms_evasion(detector):
    facts = detector.evaluate(evasive_ais_track(), assigned_observer_history())
    assert facts[-1].kind == "evasive_maneuver"
    assert facts[-1].strength == 1.0

def test_boundary_avoidance_is_not_evasion(detector):
    assert detector.evaluate(boundary_turn_track(), assigned_observer_history()) == ()
```

逃逸测试还覆盖：无 probe/track UAV、距离超过 6 cells、任一 3 分钟窗口少于 3 个不同时刻、任一窗口时跨少于 1.5 分钟、只转向但未远离、只加速但方向不向外、障碍 2 cells 内转向、两次连续确认、同 episode 去重、5 分钟 clear 后 rearm，以及 registry disabled 时仍可产生行为 fact 但不产生 `ais_position` evidence。

- [ ] **Step 2: 运行并确认旧单 vessel_class/单 sensor_mode 失败**

Run: `python -m pytest tests/mission/test_trajectory_features.py tests/mission/test_evasion_detector.py tests/mission/test_contact_assessor.py tests/mission/test_sensor_modes.py -q`

Expected: FAIL at new fields/state machine。

- [ ] **Step 3: 实现测线、EO 和辐射特征**

```python
survey_motion = (
    minutes_in_regulated_area >= cfg.min_observed_duration_min
    and median_speed_kn <= cfg.observed_speed_max_kn
    and reversal_count >= cfg.min_reversal_count
)
independent_families = {
    item.family for item in evidence
    if item.passes_quality_gate
    and item.family in {"eo_activity", "survey_motion", "radiation_activity"}
}
confirmed = len(independent_families) >= 2 and bool(
    independent_families & {"eo_activity", "survey_motion"}
)
```

实现 `PassivePosition`/contact 关联：先按 `emitter_track_id` 建立或取得 signal contact；对可与 EO/SAR/AIS 接触合并的候选，先把接触匀速投影到 `observed_at_min`，再计算其位置与 `position_cells` 的欧氏距离。唯一最近候选需满足 `distance<=position_association_radius_cells`；距离并列时返回 `ambiguous_contact_association` 且不合并。按 contact 聚合 10 分钟内不同 burst ID，至少两个才生成 `radiation_activity`。测试 emitter track 稳定关联、阈值内唯一合并、门限外不合并、等距歧义拒绝、单 burst 不足、两个 burst 通过，以及 `eo_class` 不能计入 activity family。

Prompt 必须说明 AIS silent 不能判类别/违规；输出 schema 分别验证 class/activity evidence ID 属于当前 snapshot 和允许的证据族，两个 confidence 独立且均在 [0,1]。

- [ ] **Step 4: 实现 AIS 逃逸状态机和高价值 fact**

按 D §7.6.2 实现 `clear/candidate/confirmed/cooldown`。使用 `[t-6,t-3)` 和 `[t-3,t]` 两窗；每窗至少 3 个不同时刻且时跨至少 1.5 分钟，否则返回 `insufficient_window_support`。对两窗分别用时间间隔作权重的最小二乘拟合速度，角度用最小夹角；船速 cells/min 与 kn 通过 `grid.cell_size_km` 统一换算。只有指定 probe/track UAV 在 6 cells 内时评估，取距离最近的合法 UAV，不因 UAV 列表顺序改变结果。两次连续命中才产生 `evasion_episode_id`；第一条 fact 设 `episode_started=True`，同 episode 后续点设 false 并只 supersede 空间证据。输出交给 T6 `InformationUpdatePolicy`，检测器不得直接改 `V`。

- [ ] **Step 5: 实现 SAR/EO 切换计时和 passive 恒开不变量**

任务安装只调用 `request_active_mode()`；切换阶段不产生 SAR/EO 样本。任何命令试图关闭 passive 均在验证层拒绝。

- [ ] **Step 6: 集成每步观测顺序并运行测试**

Run: `python -m pytest tests/mission/test_sensor_modes.py tests/mission/test_evasion_detector.py tests/mission/test_contact_assessor.py tests/mission/test_contact_store.py tests/mission/test_trajectory_features.py tests/env/test_simulation_integration.py -q`

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add src/mission/evasion_detector.py src/mission/trajectory_features.py \
  src/mission/contact_assessor.py \
  src/mission/contact_store.py src/mission/contracts.py \
  src/mission/prompts/contact_assessor.txt src/env/eo_sensor.py \
  src/env/uav_entity.py src/control/common/observation.py src/env/simulation.py \
  tests/mission/test_trajectory_features.py tests/mission/test_contact_assessor.py \
  tests/mission/test_contact_store.py tests/mission/test_evasion_detector.py \
  tests/mission/test_sensor_modes.py tests/env/test_simulation_integration.py
git commit -m "feat: detect evasive tracks and separate observed activity"
```

## Task 8: 接力证据与任务生命周期

**Files:**
- Modify: `src/mission/contracts.py`
- Modify: `src/mission/task_catalog.py`
- Modify: `src/mission/mission_scheduler.py`
- Modify: `src/env/simulation.py`
- Modify: `src/schedule/trigger_manager.py`
- Create: `tests/mission/test_handoff.py`
- Modify: `tests/mission/test_mission_scheduler.py`

**Interfaces:**
- Produces: `HandoffAttempt(handoff_id, contact_id, source_uav_id, successor_uav_id, evidence_id, required_at_min, assignment_deadline_min, lock_deadline_min, assignment_committed_at_min, eo_lock_acquired_at_min, state, failure_reason)`；原 contact task 持续存在。

- [ ] **Step 1: 写“退出不等于完成”和投影测试**

```python
def test_returning_observer_keeps_contact_task_incomplete(system):
    system.observe_contact("C1", uav="U1")
    system.force_return("U1")
    assert system.task("C1").status == "handoff_required"
    assert system.task("C1").completed_at_min is None

def test_handoff_center_uses_contact_projection_not_uav_or_base(system):
    handoff = system.force_return("U1")
    assert handoff.mean == pytest.approx(last_contact_pos + velocity * delta_t)
```

- [ ] **Step 2: 运行并确认空间语义/状态缺失**

Run: `python -m pytest tests/mission/test_handoff.py tests/mission/test_mission_scheduler.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现 handoff 状态机和 EvidenceRecord**

点/椭圆状态按匀速模型投影；只有 bearing 时创建方向搜索任务。连续重复 fuel warning 幂等更新同一 handoff attempt，不重复指标分母。assignment 5 分钟或 EO lock 10 分钟超时后把 attempt 标为 failed，contact task 保持未完成；下一触发为同一中断创建新 attempt，指标仍只计一个中断分母。

- [ ] **Step 4: 接入可行边与抢占规则**

handoff 优先级高于普通 search，可抢占普通 search；不得抢占 return/refuel/safety/已有 probe/track。成功必须等后继 UAV 有效 EO lock，assignment commit 只能进入 `handoff_pending`。

- [ ] **Step 5: 运行测试并提交**

Run: `python -m pytest tests/mission/test_handoff.py tests/mission/test_mission_scheduler.py tests/mission/test_failure_paths.py -q`

Expected: PASS。

```bash
git add src/mission/contracts.py src/mission/task_catalog.py \
  src/mission/mission_scheduler.py src/env/simulation.py \
  src/schedule/trigger_manager.py tests/mission/test_handoff.py \
  tests/mission/test_mission_scheduler.py
git commit -m "feat: preserve observation tasks through UAV handoff"
```

## Task 9: 完整可行候选池和公平 Prompt 窗口

**Files:**
- Modify: `src/schedule/candidate_extractor.py`
- Modify: `src/mission/task_catalog.py`
- Create: `src/mission/prompt_window.py`
- Modify: `src/schedule/task_allocator.py`
- Modify: `src/schedule/trigger_manager.py`
- Modify: `src/mission/mission_scheduler.py`
- Modify: `src/schedule/output_validator.py`
- Modify: `src/mission/llm_gateway.py`
- Modify: `src/mission/prompts/mission_scheduler.txt`
- Modify: `tests/schedule/test_candidate_extractor.py`
- Modify: `tests/schedule/test_trigger_manager.py`
- Create: `tests/mission/test_prompt_window.py`
- Modify: `tests/mission/test_mission_scheduler.py`
- Modify: `tests/schedule/test_output_validator.py`
- Modify: `tests/mission/test_llm_gateway.py`

**Interfaces:**
- Consumes: T6 `InformationSnapshot/InfoFieldDelta`、T7/T8 紧急 evidence/task。
- Produces: `CandidatePool(candidates, unschedulable_cells, geometry_version, information_version)`；`PromptWindow.select(tasks, capacity, cycle) -> PromptSelection`；稳定 `eligible_since_min/skip_cycles`；同版本 `PlanningSnapshot -> AssignmentBatch`。
- Contract migration: `TaskCandidate`、`PlanningSnapshot`、任务选择回复与 `AssignmentBatch` 都增加 `information_version:int`；该值由调度器从冻结快照附加，模型只能原样回显，不得自行生成或递增。

- [ ] **Step 1: 写覆盖完整性和无饥饿测试**

```python
def test_every_schedulable_high_value_cell_has_a_candidate(pool, high_value_cells):
    covered = {cell for candidate in pool.candidates for cell in candidate.cells}
    assert high_value_cells <= covered | set(pool.unschedulable_cells)

def test_stable_candidate_enters_prompt_within_skip_bound(window):
    seen = set()
    fair_quota = 2
    bound = math.ceil(len(stable_tasks) / fair_quota)
    for cycle in range(bound):
        seen.update(t.task_id for t in window.select(stable_tasks, 8, cycle).tasks)
    assert seen == {t.task_id for t in stable_tasks}

def test_evasive_delta_triggers_one_heavy_plan_even_when_value_is_masked(planner):
    delta = planner.apply(evasive_delta(
        version=7, evidence_id="EV-1", value_changed=False, max_delta=0.0,
    ))
    first = planner.check()
    second = planner.check()
    assert first.trigger_type == "heavy"
    assert first.information_version == 7
    assert second.trigger_type == "none"
```

另测 50/25/25 配额、去重、handoff/probe/track/investigation 保留、5x5 全局 `V/S/A/cause` 摘要、候选顺序与 dict/hash 随机无关、普通候选效用公式，以及 bearing 走廊切分、`passive_position` 点区域、逃逸投影区的确定性 task 构造。触发测试覆盖：urgent 绕过普通 cooldown、同 evidence/episode 去重、非紧急 max delta 0.05、跨 `candidate_value_threshold`、1 分钟普通 cooldown 合并原因、纯衰减未跨阈值不逐步 heavy、同步多个 urgent 只调一次 LLM。

- [ ] **Step 2: 运行并确认当前 Top-K 截断失败**

Run: `python -m pytest tests/schedule/test_candidate_extractor.py tests/schedule/test_trigger_manager.py tests/mission/test_prompt_window.py -q`

Expected: FAIL，旧 `clusters[:K*4]`/final K 无法满足覆盖或公平性。

- [ ] **Step 3: 实现几何缓存和完整池**

```python
for width, height in legal_shapes(cfg):
    for col in range(cols - width + 1):
        for row in range(rows - height + 1):
            bbox = BBox(col, row, col + width, row + height)
            if geometry_feasible(bbox, masks, coverage_planner):
                candidates.append(score_candidate(bbox, value, info, seen))
```

静态 geometry feasibility 按 obstacle/land version 缓存；动态占用和值必须从同一 `InformationSnapshot` 重新计算，每个 candidate 带该 `information_version`。不能在完整性检查前按总价值截断。

为 `V`、seen 和 occupied 构建 summed-area table，以四次数组读取计算 bbox sum/count；加入 30x30/默认 shapes 的 microbenchmark，固定 fixture transport 下 candidate pool + prompt window 的 p95 必须小于 200ms，为 2 秒总预算保留模型时间。

普通搜索候选的窗口效用固定为 `0.5*mean(V)+0.3*max(V)+0.2*unseen_fraction`。按 D §7.11 另实现证据区域转换：bearing 将 `K>=0.1` 走廊切为有序 `direction_search`；`passive_position` 以精确点所在 cell 向外扩 1 cell 生成 `investigation`；逃逸用预计到达时刻的航迹投影协方差 bbox + 1 cell 生成 `probe`；type_ii/violation 使用投影接触区生成或刷新 `track`。小区域对称扩展至最小可执行面积，边界附近向内平移；修复仍不可行时记录原因，禁止移动估计中心。

- [ ] **Step 4: 实现公平窗口和全局摘要**

```python
quotas = {
    "fair": max(1, capacity // 4),
    "geography": capacity // 4 if capacity >= 3 else 0,
}
quotas["utility"] = capacity - quotas["fair"] - quotas["geography"]
fair = fifo.take_after_cursor(tasks, quotas["fair"])
fifo.advance_past(fair)
```

Prompt 日志记录每个 task 的入选来源和连续未入选周期。稳定集合下无饥饿测试必须通过，运行时发布 `fairness_bound_cycles=ceil(M/q_fair)`；`q_fair` 必须大于 0。新候选从队尾加入，已消失候选从队列删除，不能以价值重新排序 FIFO 队列。紧急 `evasive_maneuver/passive_position/violation/handoff` 任务在搜索配额外保留，Prompt 必须提供 cause kind、强度、age、空间参数和 changed bbox；仅对本身存在不确定度的证据提供不确定性，不得提供真值类别。

- [ ] **Step 5: 保持 LLM 选 task ID、确定性匹配 UAV**

验证器拒绝窗口外 ID；匹配 cost 增加 fuel ratio、切换耗时和合法抢占 penalty。先最大基数后最小成本，不能退化为贪心导致可行任务丢失。

mission scheduler Prompt 要求 LLM 按“紧急接力/异常 -> 本轮覆盖价值 -> 最久未服务 -> 转场/切换代价”选择 task ID 集合并说明 defer reason。validator 确保选中区域两两不重叠、不与活动空域冲突、数量不超过本轮可用资源；本轮不做全图无缝镶嵌。LLM 可输出允许抢占的 UAV ID，但不直接指定最终任务-UAV 对。

`TaskAllocator` 在 `InformationSnapshot` 冻结的同一语句后设置 `absolute_deadline=perf_counter()+2.0`，并把同一 `information_version` 和 deadline 传给候选构建、Prompt、LLMGateway、validator 和 matcher。LLMGateway 最多首次 + 2 次纠错，单次 transport timeout 为 `max(0, absolute_deadline-postprocess_reserve-now)`；剩余 <=0 立即失败。validator/matcher 在进入和返回时检查 `perf_counter()<=absolute_deadline`，且拒绝 candidate/response/batch 中的版本不一致。任何超时均返回 `decision_deadline_exceeded` 且丢弃未提交 batch。失败保持已提交任务并安排 1 仿真分钟后的新 trigger，不调用确定性规则替模型选任务。

- [ ] **Step 6: 运行调度回归与提交**

Run: `python -m pytest tests/schedule/test_candidate_extractor.py tests/schedule/test_trigger_manager.py tests/mission/test_prompt_window.py tests/mission/test_task_catalog.py tests/mission/test_mission_scheduler.py tests/schedule/test_task_allocator.py tests/schedule/test_output_validator.py tests/mission/test_llm_gateway.py -q`

Expected: PASS。

```bash
git add src/schedule/candidate_extractor.py src/mission/task_catalog.py \
  src/mission/prompt_window.py src/schedule/task_allocator.py \
  src/schedule/trigger_manager.py src/mission/mission_scheduler.py \
  src/schedule/output_validator.py \
  src/mission/llm_gateway.py src/mission/prompts/mission_scheduler.txt \
  tests/schedule/test_candidate_extractor.py tests/schedule/test_trigger_manager.py \
  tests/mission/test_prompt_window.py \
  tests/mission/test_task_catalog.py tests/mission/test_mission_scheduler.py \
  tests/schedule/test_task_allocator.py tests/schedule/test_output_validator.py \
  tests/mission/test_llm_gateway.py
git commit -m "feat: expose a fair complete search pool to planning"
```

## Task 10: 初始化船舶编辑后端事务

**Files:**
- Create: `src/mission/vessel_commands.py`
- Modify: `src/env/simulation.py`
- Modify: `src/vis/backend/server.py`
- Modify: `src/vis/backend/frame_builder.py`
- Create: `tests/mission/test_vessel_commands.py`
- Modify: `tests/env/test_server_runtime.py`
- Modify: `tests/env/test_frame_publisher.py`

**Interfaces:**
- Produces: `POST /api/vessels`、`DELETE /api/vessels/{id}`、`GET /api/vessel-commands/{id}`；幂等 queue；frame 只发布 `editing_allowed` 和 configured/actual counts；隔离的 `GET /api/scenario/vessels` 仅在编辑期发布 scenario entity ID/revision/position/class。

- [ ] **Step 1: 写 API、幂等、边界和事务测试**

```python
def test_create_command_is_applied_only_by_simulation_thread(client, engine):
    response = client.post("/api/vessels", json=valid_create_payload())
    assert response.status_code == 202
    assert len(engine.ships) == initial_count
    engine.apply_pending_vessel_commands()
    assert len(engine.ships) == initial_count + 1

def test_editing_closes_after_first_step(client, engine):
    engine.step()
    response = client.post("/api/vessels", json=valid_create_payload())
    assert response.status_code == 409
    assert response.json()["error_code"] == "editing_closed"

def test_duplicate_json_keys_are_rejected_before_model_validation(client):
    response = client.post(
        "/api/vessels",
        content=b'{"episode_id":"one","episode_id":"two"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "duplicate_json_key"
```

- [ ] **Step 2: 运行并确认 endpoint/queue 缺失**

Run: `python -m pytest tests/mission/test_vessel_commands.py tests/env/test_server_runtime.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现队列和服务端最终校验**

复用 IntentCommandQueue 的锁、容量、command hash 和 tombstone 模式，但使用独立命名空间。创建在副本上完成位置、间距、连续水域和 patrol route 全部验证后再 append；删除用 vessel revision CAS。

将构造函数中的 `_refresh_ais_signals(0.0)` 移到首步编辑命令应用之后。编辑关闭前不得创建 AIS contact、EvidenceRecord、task 或评价分母；增加测试断言删除船舶的 MMSI 从未进入 `ContactStore`。

- [ ] **Step 4: 实现 API 与 frame 字段**

统一把现有 `_request_object()` 改为读取 raw body，并用 `json.loads(..., object_pairs_hook=unique_object, parse_constant=reject_constant)` 在 Pydantic/字段 allowlist 之前拒绝重复键和非有限常量；所有 JSON mutation endpoint 共用该解析器。严格字段 allowlist；episode/revision 冲突返回 409，几何/路线错误返回 422，成功排队返回 202。回放或 `sim_time>0` 返回 `editing_closed`。`GET /api/scenario/vessels` 只在编辑期返回 `scenario_entity_id/revision/position/vessel_class`；关闭后返回 409，数据不写 mission frame、Prompt、ContactStore 或 blue log。

- [ ] **Step 5: 运行后端测试并提交**

Run: `python -m pytest tests/mission/test_vessel_commands.py tests/env/test_server_runtime.py tests/env/test_frame_publisher.py -q`

Expected: PASS。

```bash
git add src/mission/vessel_commands.py src/env/simulation.py \
  src/vis/backend/server.py src/vis/backend/frame_builder.py \
  tests/mission/test_vessel_commands.py tests/env/test_server_runtime.py \
  tests/env/test_frame_publisher.py
git commit -m "feat: add transactional initialization vessel editing"
```

## Task 11: 右侧组件库、地图放置/删除和证据图层

**Files:**
- Modify: `src/vis/frontend/src/App.jsx`
- Modify: `src/vis/frontend/src/App.css`
- Modify: `src/vis/frontend/src/components/RightSidebar.jsx`
- Modify: `src/vis/frontend/src/components/CanvasMap.jsx`
- Modify: `src/vis/frontend/src/renderer/layers.js`
- Modify: `src/vis/frontend/playwright.config.js`
- Modify: `src/vis/frontend/tests/mixed-maritime.spec.js`
- Modify: `src/vis/frontend/tests/acceptance.spec.js`

**Interfaces:**
- Consumes: Task 10 API/frame、bearing/position/contact frame 数据。
- Produces: 两类船舶组件；drag/drop 和 click/place；选中删除；方位扇区、条件释放位置点、AIS 更新资格。

- [ ] **Step 1: 写 Playwright 失败测试**

```javascript
test("operator places a type_ii vessel by click and deletes it", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "II 类船舶" }).click();
  await clickGrid(page, 12.5, 8.5);
  await expect.poll(() => postedCommands(page)).toHaveLength(1);
  await selectScenarioVessel(page, "scenario-vessel-9");
  await page.getByRole("button", { name: "删除选中船舶" }).click();
  await expect.poll(() => deletedVessels(page)).toContain("scenario-vessel-9");
});
```

另测 drag/drop、非法位置提示、运行/回放禁用、最长中文标签不溢出、30x30 缩放下命中测试、单方位不画点、多机位置释放后画精确位置点。

- [ ] **Step 2: 运行并确认控件缺失**

Run: `npm --prefix src/vis/frontend exec -- playwright test`

Expected: FAIL at vessel library locator。

- [ ] **Step 3: 实现组件库与放置状态**

用现有图标库（若已启用）或已有按钮风格；组件为实际拖拽 source，地图是 drop target。放置状态有明确取消按钮/图标和选中态，不用说明性大段文字。前端坐标只作预览，最终以 command result 为准。

在 `playwright.config.js` 增加 `webServer`，命令为 `npm run dev -- --host 127.0.0.1`，URL 使用现有 `baseURL`，`reuseExistingServer: true`；这样计划中的 Playwright 命令无需人工另开服务进程。

- [ ] **Step 4: 实现地图选择与证据图层**

编辑期船舶 hit testing 使用隔离 scenario snapshot 和稳定像素半径；删除前使用选中 scenario entity ID/revision，编辑关闭即清空该 snapshot。运行期只渲染 contacts/evidence。bearing 画向前扇区，长度由接收范围确定；`PassivePosition` 只画条件释放的精确位置点，不渲染协方差椭圆，也不读取其他环境真值。

- [ ] **Step 5: build、Playwright 和截图检查**

Run: `npm --prefix src/vis/frontend run build`

Expected: exit 0。

Run: `npm --prefix src/vis/frontend exec -- playwright test`

Expected: PASS。用 1440x900、1024x768、390x844 截图确认侧栏、地图、底栏不重叠，组件文字不溢出。

- [ ] **Step 6: 提交**

```bash
git add src/vis/frontend/src/App.jsx src/vis/frontend/src/App.css \
  src/vis/frontend/src/components/RightSidebar.jsx \
  src/vis/frontend/src/components/CanvasMap.jsx \
  src/vis/frontend/src/renderer/layers.js \
  src/vis/frontend/playwright.config.js \
  src/vis/frontend/tests/mixed-maritime.spec.js \
  src/vis/frontend/tests/acceptance.spec.js
git commit -m "feat: add vessel editing and passive evidence visualization"
```

## Task 12: 指标、时延和审计日志

**Files:**
- Modify: `src/mission/outcome_evaluator.py`
- Modify: `src/mission/episode_logger.py`
- Modify: `src/schedule/task_allocator.py`
- Modify: `src/env/simulation.py`
- Modify: `scripts/evaluate_mixed_maritime.py`
- Modify: `tests/mission/test_outcome_evaluator.py`
- Modify: `tests/mission/test_episode_logger.py`
- Modify: `tests/mission/test_evaluation_cli.py`

**Interfaces:**
- Produces: D §11.2 指标、confusion matrix、handoff event ledger、`decision_latency_seconds` 分解和 N/A 规则；结束时间是 AssignmentBatch ready 或明确失败返回，两者都能计时。每轮另产生 `observation_id -> evidence_id -> information_version -> task_id -> decision_id -> assignment_id` 闭环审计链。

- [ ] **Step 1: 写指标分母和计时测试**

```python
def test_unknown_classification_counts_as_error(evaluator):
    evaluator.add_classification(truth="type_ii", predicted="unknown", eligible=True)
    result = evaluator.summary()
    assert result["type_ii_recall"] == 0.0

def test_zero_denominator_is_na_not_success(evaluator):
    assert evaluator.summary()["handoff_success_rate"] is None
```

另测同一中断只计一个分母、无可行 successor 的中断单列 excluded reason、EO lock 恢复窗口、响应计时包含 retry、timeout 计失败且保留实际时长。闭环 ledger 测试断言六段 ID 可顺向/反向关联，同一决策中 candidate/Prompt/AssignmentBatch 的 `information_version` 一致，失败决策只到 decision ID 且有终止原因。

持续观察率测试用 evaluator-only 的隐藏 `survey` 区间构造分母；把算法输出保持 unknown 时，分母必须不变且相应分钟 numerator 为 0，证明算法不能通过延迟确认缩小 required-observation 时间。

- [ ] **Step 2: 运行并确认旧 evaluator 口径失败**

Run: `python -m pytest tests/mission/test_outcome_evaluator.py tests/mission/test_episode_logger.py tests/mission/test_evaluation_cli.py -q`

Expected: FAIL at new metric fields/semantics。

- [ ] **Step 3: 实现混淆矩阵和事件 ledger**

```python
type_i_recall = tp_type_i / eligible_type_i if eligible_type_i else None
type_ii_recall = tp_type_ii / eligible_type_ii if eligible_type_ii else None
balanced_accuracy = (
    (type_i_recall + type_ii_recall) / 2
    if type_i_recall is not None and type_ii_recall is not None else None
)
```

输出 raw numerator/denominator、N/A reason 和阈值判断，不只输出百分比。

- [ ] **Step 4: 在快照冻结和 batch ready 处打单调墙钟时间戳**

使用 `time.perf_counter()`；记录 `llm_seconds/validation_seconds/matching_seconds/total_seconds`。不得用仿真时间替代响应时延。

- [ ] **Step 5: 运行评估测试并提交**

Run: `python -m pytest tests/mission/test_outcome_evaluator.py tests/mission/test_episode_logger.py tests/mission/test_evaluation_cli.py -q`

Expected: PASS。

```bash
git add src/mission/outcome_evaluator.py src/mission/episode_logger.py \
  src/schedule/task_allocator.py src/env/simulation.py \
  scripts/evaluate_mixed_maritime.py tests/mission/test_outcome_evaluator.py \
  tests/mission/test_episode_logger.py tests/mission/test_evaluation_cli.py
git commit -m "feat: define maritime mission acceptance metrics"
```

## Task 13: 端到端集成、性能门和文档迁移

**Files:**
- Create: `tests/mission/test_maritime_acceptance.py`
- Modify: `scripts/evaluate_mixed_maritime.py`
- Modify: `README.md`
- Modify: `docs/SYSTEM_PARAMS.md`
- Modify: `docs/MIXED_MARITIME_VALIDATION.md`
- Modify: `src/vis/frontend/tests/acceptance.spec.js`

**Interfaces:**
- Consumes: Tasks 1-12 全部契约。
- Produces: 可复现验收报告；AIS 逃逸和多机被动位置两条完整三层闭环 trace；真实 LLM 与 fixture 结果分离；新术语文档无旧运行时语义。

- [ ] **Step 1: 写六条端到端失败场景**

```python
def test_single_bearing_updates_direction_value_without_contact_position(scenario):
    result = scenario.observe_emitter(observer_ids=("UAV-1",))
    assert len(result.bearings) == 1
    assert result.positions == ()
    assert result.point_contacts == ()
    assert result.value_ahead > result.value_behind

def test_two_same_group_detections_release_true_position_and_task(scenario):
    result = scenario.observe_emitter(observer_ids=("UAV-1", "UAV-2"))
    assert len(result.positions) == 1
    assert result.positions[0].position_cells == result.emitter_true_position_at_sample
    assert result.positions[0].source_observation_ids == tuple(
        item.observation_id for item in result.bearings
    )
    assert result.investigation_task.feasible_uav_ids
    assert result.value_at_position_after > result.value_at_position_before
    assert result.info_version_after == result.info_version_before + 1
    assert result.planning_snapshot.information_version == result.info_version_after

def test_ais_evasion_refreshes_value_and_drives_llm_assignment(scenario):
    result = scenario.run_ais_evasion_trace(mmsi="AIS-OPEN-1")
    assert result.evidence.kind == "evasive_maneuver"
    assert result.value_at_contact_after > result.value_at_contact_before
    assert result.trigger.trigger_type == "heavy"
    assert result.llm_selected_task_id == result.investigation_task.task_id
    assert result.assignment.information_version == result.info_delta.version
    assert result.assignment.uav_id in result.investigation_task.feasible_uav_ids

def test_type_i_confirmation_disables_only_future_ais_value_updates(scenario):
    first = scenario.ingest_ais_and_snapshot_value("CIVIL-MMSI", at_min=1.0)
    scenario.confirm_type_i("CIVIL-CONTACT", at_min=2.0)
    second = scenario.ingest_ais_and_snapshot_value("CIVIL-MMSI", at_min=3.0)
    assert second.contact_revision > first.contact_revision
    assert second.new_ais_evidence_ids == ()
    assert first.ais_evidence_ids <= second.retained_evidence_ids

def test_fuel_return_handoff_recovers_eo_without_completing_on_departure(scenario):
    handoff = scenario.trigger_fuel_handoff("UAV-1", contact_id="type_ii-1")
    assert handoff.task_status == "handoff_required"
    assert handoff.contact_completed_at_min is None
    recovered = scenario.advance_until_successor_eo_lock(handoff.handoff_id)
    assert recovered.task_status == "observing"
    assert recovered.successor_uav_id != "UAV-1"

def test_observation_execution_closes_back_into_next_information_version(scenario):
    first = scenario.run_passive_position_to_assignment()
    second = scenario.execute_assignment_until_valid_eo(first.assignment.assignment_id)
    assert second.observation_ids
    assert second.info_delta.version > first.assignment.information_version
    assert second.info_delta.cause_evidence_ids
```

本 task 在同一测试文件定义 `scenario` fixture，使用 fixture transport 和公开观测注入方法；fixture 只能注入传感器输出，不能直接设置蓝方类别、违规结论或任务完成。测试可由 evaluator-only 接口读取采样时刻辐射源真值，仅用于断言条件释放坐标正确；该真值不得进入蓝方 snapshot、Prompt 或默认日志。

本 task 对两条三层 trace 逐段断言 `observation_id/evidence_id/info_version/task_id/decision_id/assignment_id`，不允许只断言最终“有任务”。再加入 12 UAV/20 船比例场景、480 分钟船舶不越界、候选公平窗口、多次 reset 命令 tombstone、运行期编辑拒绝。

- [ ] **Step 2: 运行集成测试确认尚有跨模块失败**

Run: `python -m pytest tests/mission/test_maritime_acceptance.py -q`

Expected: 初次 FAIL；逐项修复接线，不在测试中绕过 LLM/传感器/控制契约。

- [ ] **Step 3: 运行后端全套回归**

Run: `python -m pytest -q`

Expected: 全部 PASS；真实 API 依赖测试按项目既有 marker 明确区分，不能把 skip 算通过。

- [ ] **Step 4: 运行静态检查和前端全套**

Run: `python -m ruff check src tests scripts`

Expected: exit 0。

Run: `npm --prefix src/vis/frontend run build`

Expected: exit 0。

Run: `npm --prefix src/vis/frontend exec -- playwright test`

Expected: exit 0，全部 Playwright PASS。

- [ ] **Step 5: 运行确定性性能与验收批次**

Run: `python scripts/evaluate_mixed_maritime.py --config configs --seeds 101,102,103,104,105 --repeat 3 --output outputs/maritime-alignment-fixture.json --transport fixture`

Expected: 报告包含每个指标 numerator/denominator、p50/p95/max latency、N/A reason、船舶边界违规数 0、候选未覆盖且无原因数 0、闭环 trace 断链数 0、跨版本决策数 0，并分别统计逃逸与 passive-position 事件从 fact 到 AssignmentBatch 的时延。Fixture 结果必须标记 `transport=fixture`，不得声明真实模型达标。

Run: `python scripts/evaluate_mixed_maritime.py --config configs --seeds 201,202,203,204,205 --repeat 3 --output outputs/maritime-alignment-live.json --transport live`

Expected: 只在配置真实 API key 的验收环境运行；201-205 是不参与本轮调参的独立 holdout，但不是秘密 seed。报告 `transport=live`。任何模型失败/timeout 保留为 operational failure，不删除样本。D §11.2 四项成功率均 >=0.85 且非故障注入 planning samples 每次 <=2.0s 才能声明达标。

- [ ] **Step 6: 更新文档并扫描旧术语**

Run: `rg -n "II 类船舶|船舶|vessel_class|target_ship_count|initial_ship_count|count_max|sensor\.radar" README.md docs src configs tests`

Expected: 仅迁移说明、旧 replay adapter 或明确历史文档允许命中；运行时、UI、Prompt 和当前说明不再使用旧语义。

- [ ] **Step 7: 最终提交**

```bash
git add tests/mission/test_maritime_acceptance.py scripts/evaluate_mixed_maritime.py \
  README.md docs/SYSTEM_PARAMS.md docs/MIXED_MARITIME_VALIDATION.md \
  src/vis/frontend/tests/acceptance.spec.js
git commit -m "test: validate maritime requirements end to end"
```

## 最终审查门

- [ ] 对照 D §1-§13 逐项标注实现 task 和测试；不得有未映射需求。
- [ ] 按 writing-plans 的 “No Placeholders” 禁用模式扫描计划和实现记录，不得用待补、稍后实现、泛化错误处理或引用相似任务来替代具体行为。
- [ ] 核对 `vessel_class/activity`、`PassivePosition` 字段、AIS registry key、bbox 半开语义、单位和错误码在所有 task 一致。
- [ ] 核对默认 frame/Prompt/blue log 不含 `ShipTruth`、activity schedule、physical ship ID 或未满足 D08 门槛的 emitter position；已释放的 `PassivePosition` 只携带位置及来源观测 ID，不携带类别/活动真值。
- [ ] 核对船舶数量、UAV 数量、UI 列表和 StateManager 数量均来自同一个实际配置/场景状态。
- [ ] 核对单机 bearing 测试明确断言“无位置”；只有同一 sample/source/burst 的至少两架不同 UAV 才释放真实位置，任一分组条件不一致都明确断言“无位置”。
- [ ] 核对接力 assignment 与 EO 恢复分开计时，原 UAV 返航不完成 contact task。
- [ ] 核对 D01-D13 均有对应 task 和失败测试；特别确认逃逸只是高价值复查证据，不单独定性违法。
- [ ] 核对两条三层端到端 trace 的六段 ID 完整，价值在事件中心数值上升，候选/Prompt/决策/分配的 `information_version` 一致。
- [ ] 核对真实模型报告与 fixture 报告分开，未运行 live 时明确写“未验证”，不得以单元测试推断 85%/2 秒达标。

计划批准后的推荐执行方式：使用 `subagent-driven-development` 逐任务执行并在每个 commit 后做规范和质量双审；若不使用多代理，则用 `executing-plans` 按 Task 1-13 顺序分批执行，每批不跨越失败测试或审查门。
