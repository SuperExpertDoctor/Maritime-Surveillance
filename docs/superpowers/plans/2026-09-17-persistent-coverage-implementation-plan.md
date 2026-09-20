# Persistent Coverage Implementation Plan

> **For agentic workers:** 使用 `executing-plans` 按任务串行实施；仅在用户另行要求并行代理时改用 `subagent-driven-development`。每个任务采用 `- [ ]` 记录实施、测试和审查。Luna max 从配套执行入口开始，不能只阅读最后一项 UI 任务。

**Goal:** 修复真实 SAR 覆盖闭环，建立全域待办和可验证的持续覆盖指标，并在右侧随实时/回放帧刷新。

**Architecture:** StateManager 持有独立 SAR 覆盖统计；执行端传递真实扫描几何与足迹；覆盖服务验收完成；调度在现有 LLM 选择前建立公平候选与可行资源下界；前端消费同一权威 frame。

**Tech Stack:** Python 3、dataclasses、NumPy、pytest；现有 React 18/Vite、Playwright；不新增运行时依赖。

## Global Constraints

- 本计划依据 [设计](../specs/2026-09-17-persistent-coverage-design.md)，16 个 tasks 全部属于本次目标；不是只实现 T14。
- 默认主窗口 60 min，可显示 30/60/120；左开右闭 `(t-W,t]`；主分母为 episode 固定海域。
- SAR 扫描与 EO、AIS、ESM 分开；原 `coverage_pct` 保留历史含义。
- 无效观测、任务框、计划足迹、航迹完成不得写入真实覆盖；不得伪造 full coverage。
- 生产继续 LLM 决策；不因 API 故障安装未批准的规则任务；red 方旧有效计划以原规则使用，无有效计划暂停。
- 不放宽物理/安全/航程/观测边界，不读取隐藏船类；不直接设置位姿让端到端测试“通过”。
- 新模块必须 episode 隔离；所有 frame 读操作纯且幂等；无 NaN/Infinity JSON。
- 保留配置、公开接口和历史回放兼容；旧帧缺指标显示缺失，不显示 0。
- 测试默认 fixture，无外部网络；live 验收单列，模型不可用只能记 BLOCKED，不能换 fixture 后记 live PASS。
- 不修改历史 JSONL；不将数百 MB 日志复制进 tests；不调整验收阈值以消除失败。
- 每个 Task 必须先出现预期失败/基线缺陷证据，再实现，再验证。测试引用本计划新接口时，红灯应为接口缺失或行为断言，不接受无关依赖错误冒充红灯。
- 文档中的代码块为明确的接口、核心算法和必须实现的测试；生产接线按各 Task 列出的符号和顺序修改，不复制整个旧文件覆盖用户改动。对未被原日志证明的控制根因，T05 的闭环复现是修改前置条件。

## 0. 仓库与运行约定

实施前执行并保存输出：

```bash
pwd
git status --short
git rev-parse HEAD
python -c "import sys, numpy, pytest; print(sys.version); print(numpy.__version__)"
node --version
```

检查工作树及上级 AGENTS.md；如果用户已有改动，保留并记录。使用独立工作树的决定放在真正实施时。此设计基线是 `9a0fb2c`，当前符号与实际文件优先于任何旧行号。

单元测试命令从仓库根目录运行 `python -m pytest ...`；前端从 `src/vis/frontend` 运行 `npm run build`、`npx playwright test ...`。现有 Playwright 配置会启动 fixture 后端及 Vite。不得为安装浏览器修改 production 代码；环境缺浏览器记录为基础设施阻塞。

已有源码：`src/mission/config.py`、`src/mission/contracts.py`、`src/mission/information_update.py`、`src/mission/task_catalog.py`、`src/mission/mission_scheduler.py`、`src/mission/prompt_window.py`、`src/schedule/task_allocator.py`、`src/schedule/state_manager.py`、`src/control/common/{contracts,factory,coordinator,safety,executor}.py`、`src/control/heuristic/{coverage,base,task_flow}.py`、`src/utils/coverage_planner.py`、`src/env/{simulation,sar_sensor,uav_entity}.py`、`src/vis/backend/{frame_builder,replay_adapter}.py`、`src/vis/frontend/src/{App.jsx,components/RightSidebar.jsx,App.css}`。

新生产文件仅四个：`src/mission/coverage_metrics.py`、`src/mission/coverage_service.py`、`src/mission/coverage_policy.py`、`src/control/heuristic/coverage_guidance.py`；新增 UI 文件 `src/vis/frontend/src/components/CoveragePanel.jsx`。其它代码在既有职责内接线，不另建“万能 orchestrator”。

## 1. 任务依赖及交付门禁

| Task | 交付 | 前置 |
|---|---|---|
| T01 | 独立计数 oracle、审计基线、闭环测试辅助接口 | 无 |
| T02 | 配置与 SAR 覆盖统计器 | T01 |
| T03 | 真足迹→统计器→frame/回放 | T02 |
| T04 | SAR 几何参数与左右视控制契约 | T01 |
| T05 | 连续跟随与实际扫描闭环 | T04 |
| T06 | 进展看门狗和路径状态修复 | T05 |
| T07 | 按本次真实足迹验收搜索任务 | T03,T06 |
| T08 | 故障任务原子释放和停用 | T07 |
| T09 | 全域责任、稳定地理键、漏格碎片 | T02,T07,T08 |
| T10 | 单次候选窗口及先公平后几何 | T09 |
| T11 | LLM 搜索资源保底约束 | T10 |
| T12 | Prompt 压缩和真实分阶段时延 | T10 |
| T13 | 连续失败暂停与按角色重试 | T12 |
| T14 | 右侧覆盖面板、实时/回放/断连测试 | T03,T08 |
| T15 | 独立复算、长时场景与评估输出 | T01–T13 |
| T16 | 全套回归、浏览器验收和最终报告 | T14,T15 |

推荐严格按编号。任务提交应只包含对应改动，提交建议名位于每项末尾；不要求在未通过验证时提交。失败停留当前任务修复，不将任务标成完成。

## T01：冻结证据并建立独立测试 oracle

**Files:** Create `tests/mission/coverage_helpers.py`, `tests/mission/test_coverage_oracle.py`, `tests/fixtures/coverage_audit_baseline.json`; Create `docs/validation/persistent-coverage-execution.md`（实施期间的实际执行记录，与本计划分开）。

**Interfaces:**

- `oracle_coverage(events, fixed_cells, *, now_min, window_min, cell_size_km) -> dict`：events 只含 `{time, source, cells}`，用 Python set 独立统计，不导入生产 CoverageMetrics。
- `make_coverage_rig(*, bbox, start_pose, swath_cells=1.5, dt_min=1.0) -> CoverageRig`（T04 补齐几何契约后使用）：rig 包含 controller、entity、observations、safety、executor、state；`tick() -> dict` 返回 before/after pose、phase、applied command、footprint（真实坐标列表）、progress_cells、sar_imaging、distance_cells、max_speed_cells_min、obstacle_intersection；`run(max_minutes) -> list[dict]` 每 tick 走真实 command→safety→execute→sensor。

- [ ] 用下面完整 oracle 实现建立独立判断口径；events 可为 list，不需事件存储框架。

```python
def oracle_coverage(events, fixed_cells, *, now_min, window_min, cell_size_km):
    domain = set(map(tuple, fixed_cells))
    recent = set()
    for event in events:
        if event['source'] == 'sar' and now_min-window_min < event['time'] <= now_min:
            recent.update(map(tuple, event['cells']))
    covered = recent & domain
    return {
        'covered_cells': len(covered),
        'covered_area_km2': len(covered)*cell_size_km**2,
        'coverage_pct': 100*len(covered)/len(domain) if domain else None,
    }
```

- [ ] 写基线测试：4 格域中 t=0 扫 A，t=1 扫 B/C，t=59 重扫 B，t=60 EO 扫 D。t=60、W=60 结果恰为 B/C=50%；t=119、W=60 结果 0（t=59 在左边界）；t=59.999 时 B 仍在。重复事件不重复面积，乱序 events 不改变集合结果。
- [ ] 冻结手工小基线 JSON，字段包含两原日志路径、seed=42、common_end_min=177、legacy `coverage_pct` 16.7182662539/3.4055727554、SAR60 counts 53/1、dynamic denominator646、fixed671、new_frames500、unique_sim_times177。浮点以 count/denominator 为真值，不复制近似小数作为容差 0 的断言。
- [ ] 编写 CoverageRig：使用 `UAVEntity`、`ObservationProvider.build`、`SafetyEnvelope.apply`、`UAVDynamicsExecutor.execute`、`SARSensor.compute_swath_footprint`。仅初始化时设置 start_pose，之后禁止写 `_col/_row/heading_rad`。局部夹具用空 obstacle_mask、明确 bbox，速度=160/10/60 cells/min；dt 取 1.0 和 0.25。所有 tick 维护 current_time、fuel 和 actual motion；不可导入旧 V07 人为移动 UAV 的 validation hook。
- [ ] 此时只验证 oracle 测试；rig 的成像通过条件留给 T04/T05，现状缺陷须保留。

```bash
python -m pytest tests/mission/test_coverage_oracle.py -q
```

**验收:** 独立 oracle 自身通过；基线记录可追溯，执行记录说明旧 audit 不是新实现通过证据。Commit: `test: establish independent persistent coverage oracle`。

## T02：配置和覆盖统计器

**Files:** Create `src/mission/coverage_metrics.py`, `tests/mission/test_coverage_metrics.py`; Modify `src/mission/config.py`, `src/schedule/config_loader.py`, `configs/mission.yaml`, `tests/mission/test_config.py`。

**Interfaces:** 实现设计 §4 的 CoverageMetrics；新增 `CoverageConfig`，`MissionConfig.coverage` 为 kw-only 有默认值字段，ConfigLoader 显式加载并校验。现有 activity/evasion/information_update companion 不移位；manifest 在 T15 显式完整序列化。

- [ ] 添加下面明确配置（非用户最终性能承诺；阈值含义见设计）：

```yaml
coverage:
  windows_min: [30, 60, 120]
  primary_window_min: 60
  min_search_uav_fraction: 0.4
  ordinary_prompt_reserve: 8
  geometry_candidate_budget: 120
  no_progress_timeout_min: 10.0
  align_timeout_min: 8.0
  max_stall_replans: 2
  max_consecutive_decision_failures: 3
```

- [ ] 校验：windows 必须恰为去重排序后的 (30,60,120)；primary 必须在其中；fraction (0,1]；槽位/预算为正整数且槽位≤max_tasks_in_prompt、budget≥max_tasks_in_prompt；timeout 正有限；replans 非负整数；失败次数正整数；拒绝 bool 冒充 number。缺 coverage 段采用上述默认，未知字段报错。ConfigLoader 目前还校验顶层段名，必须一起加入白名单。
- [ ] 先写下面核心测试并运行红灯：

```python
import numpy as np
import pytest
from src.mission.coverage_metrics import CoverageMetrics

def test_window_boundary_and_overlap():
    m = CoverageMetrics(episode_id='e1', fixed_mask=np.ones((2,2), bool),
                        cell_size_km=10)
    m.record_sar(((0,0),), at_min=0)
    m.record_sar(((0,1),(1,0)), at_min=1)
    m.record_sar(((0,1),(0,1)), at_min=59)
    s = m.snapshot(now_min=60, feasible_mask=np.ones((2,2), bool))
    w = next(w for w in s['windows'] if w['minutes']==60)
    assert w['covered_cells']==2
    assert w['coverage_pct']==50.0
    assert w['covered_area_km2']==200.0
    assert s['unseen_pct']==25.0
    assert s['overdue_seen_pct']==25.0
    assert next(w for w in m.snapshot(now_min=119, feasible_mask=np.ones((2,2),bool))['windows'] if w['minutes']==60)['covered_cells']==0
```

- [ ] 实现 detached fixed_mask 和 `_last_sar` (-inf)。核心计算使用：

```python
valid = self._fixed & np.isfinite(self._last_sar) & (self._last_sar <= now_min)
recent = valid & (self._last_sar > now_min - window_min)
current = self._fixed & np.asarray(feasible_mask, dtype=bool)
count = int(np.count_nonzero(recent))
dynamic_count = int(np.count_nonzero(recent & current))
```

- [ ] snapshot 按设计 JSON 逐字段返回；`overdue_seen_pct = cumulative_pct - primary.coverage_pct`；unseen+cumulative=100；固定零格时比例 null、counts0；动态零格只有 dynamic pct=null。禁止 int(bool) 被当坐标，非法索引报 ValueError，固定域外有效地图格忽略。
- [ ] 增加测试：10/100/900 格；不同 cell_size；非连续 tick；窗口边界±1e-9；重复同时间 scan；越界、NaN/inf/negative 时间、倒退；浮点时刻；fixed/feasible shape 不同；传入/返回 ndarray 修改不影响实例；snapshot 两次字典相等；新 episode 零状态；`json.dumps(...,allow_nan=False)`。

```bash
python -m pytest tests/mission/test_coverage_metrics.py tests/mission/test_config.py tests/test_runtime_configuration.py -q
```

**验收:** 对 oracle 参数化比较完全一致；不依赖 I 衰减反推。Commit: `feat: add authoritative SAR rolling coverage metrics`。

## T03：实际扫描、StateManager、frame 和回放接线

**Files:** Modify `src/schedule/state_manager.py`, `src/env/simulation.py`, `src/vis/backend/frame_builder.py`, `src/vis/backend/replay_adapter.py`; Create `tests/mission/test_coverage_scan_integration.py`, `tests/env/test_coverage_frame.py`, `scripts/persistent_coverage_scenarios.py`, `scripts/capture_coverage_baseline.py`; Modify `tests/vis/test_replay_adapter.py`。

**Consumes:** T02 CoverageMetrics。**Produces:** `StateManager.configure_coverage_metrics(fixed_mask, episode_id)`、`get_persistent_coverage_stats()`；所有新 frame.coverage_metrics；审计事件 `sar_scan`。

- [ ] StateManager 初始 metrics=None（独立旧单元夹具没有地图）；getter 返回 None。Simulation 地图/基地完成后一次 configure，使用 `_intent_searchable_mask()`，不在 build_frame 偷懒推地图。
- [ ] SAR 分支先取得实际 footprint、去重，再保留原 sm.scan_cell 的 I/V 刷新，然后记录一次 `record_sar`；EO 分支不调用 record_sar。元数据从当前 coordinator task/lease 获取，不能从场景真值获取。

```python
cells = tuple(sorted({(cell.col, cell.row) for cell in footprint}))
sm.coverage_metrics.record_sar(cells, at_min=current_time)
sm.add_event('sar_scan', {
    'episode_id': self.episode_id,
    'uav_id': uav.id,
    'task_id': task.task_id if task is not None else None,
    'generation': lease.generation,
    'cells': [list(cell) for cell in cells],
})
```

`task`/`lease` 在当前 UAV 分支通过 `self.control_coordinator.active_task(uav.id)` / `current_lease(uav.id)` 取得；空 footprint 可省略事件，但不能省略该 tick 的时间推进语义。

- [ ] build_frame 加 `coverage_metrics=state.get_persistent_coverage_stats()`，include_matrices=False 也存在；getter snapshot 当前 sm.current_time，不追加历史。新 frame 时间和 metric.as_of_min 必须相同。
- [ ] replay adapter 只 `result.setdefault('coverage_metrics', None)`，不得根据 info_matrix 补造；有新字段深拷贝原样保存。错误 schema 前端处理，不改历史数值。
- [ ] 测试至少两次 SAR 与一次 EO 同格交替；断言 EO 改 I 却不延长 SAR 窗口。测试两个 UAV 重叠足迹只计一次，任务框很大但足迹 1 格只能计 1。天气改变动态分母、主分母/扫描次数不变。
- [ ] 用真实 `_update_sensors_and_detections` 的小夹具注入传感器确定性输出，仅这是传感器边界单元测试；另有 T05/T15 的真实 sensor 闭环，不能拿本夹具证明飞行覆盖。
- [ ] 测试 frame 多次发布/暂停重复帧/重置新 episode、纯 getter 不变、JSON 序列化、旧帧字段 null、新帧字段 roundtrip；新事件以 episode/uav/time 去重时不漏多个不同 UAV。

```bash
python -m pytest tests/mission/test_coverage_scan_integration.py tests/env/test_coverage_frame.py tests/env/test_mixed_frame.py tests/vis/test_replay_adapter.py tests/env/test_frame_publisher.py -q
```

- [ ] **修复算法之前采集受控基线。** 在本Task完成而T04尚未开始时，创建共享 `build_coverage_scenario(name, *, seed, transport)` 放入 `scripts/persistent_coverage_scenarios.py`：name取 `coverage-open-water`/`coverage-mixed-weather`，配置按T15两场景定义，fixture由同文件 `CoverageFixtureGateway` 包装现有 `_FixtureGateway`：red/contact/reviewer直接委托；decision_maker仅按公开payload候选/可行边、可选coverage_constraint选择合法集合，缺constraint视required=0、有constraint先满足must_service和required后填剩余。这个fixture从T03开始就兼容未来约束，短notes≤160；将源码hash写manifest，后续基线/候选必须使用同一fixture源码，不在T15换一个更聪明的代理来抬分。`scripts/capture_coverage_baseline.py`支持 `--scenario --seeds --steps --output-dir`，只用于此telemetry-only检查点，按seed循环真实engine.step/capture_frame，遇停时即结束，JSONL+完整manifest按T15布局保存；不修改算法，不伪造成功。两场景各5seeds×720min，路径分别baseline-open-water/baseline-mixed。记录此时git SHA/dirty patch hash，之后不得用新算法覆盖baseline目录。

```bash
python scripts/capture_coverage_baseline.py --scenario coverage-open-water --seeds 42 43 44 45 46 --steps 720 --output-dir outputs/evaluations/persistent/baseline-open-water
python scripts/capture_coverage_baseline.py --scenario coverage-mixed-weather --seeds 42 43 44 45 46 --steps 720 --output-dir outputs/evaluations/persistent/baseline-mixed
```

基线脚本只捕获数据，低覆盖不使脚本伪报崩溃；manifest标 `baseline_only=true` 和实际结束时间，不能声称它已经通过长时门槛。T15再用独立oracle分析这些原生事件，避免依赖旧大日志或缺字段重建。若未能完整推进，保留失败段，说明受控对照为何不完整，不能事后合成。

**验收:** 事件 oracle 与新字段相等；实时/日志共用 build_frame；算法修复前基线已留存。Commit: `feat: publish rolling coverage from actual SAR observations`。

## T04：扫描几何与控制命令契约

**Files:** Modify `src/control/common/{contracts,factory,safety,executor}.py`, `src/control/heuristic/coverage.py`, `src/env/simulation.py`, `src/control/interface.md`; Modify `tests/control/{test_contracts,test_factory,test_safety,test_executor}.py`; Create `tests/control/heuristic/test_coverage_sensor_geometry.py`。

**Consumes:** 实际 sensor.swath_width_cells/near_range_cells、UAV.R_min/sar_along_track_cells/sar_heading_tolerance_rad。**Produces:** CoverageExecutionConfig、ControlCommand 新 kw-only 三字段；明确每 scan_range 的方向。

- [ ] frozen `CoverageExecutionConfig` 六字段按设计；factory 新 `coverage_execution: CoverageExecutionConfig|None=None`，生产必须传入。Simulation 已在设置 SARSensor 后构造 factory，直接使用初始化实体共同传感器参数；若同机队参数不同，factory 改为按 uav_id 的只读 mapping，不能静默取第一架。当前同构机队用单对象且断言一致。
- [ ] ControlCommand 追加 kw-only 默认None，避免破坏现有位置参数。三个字段为 sar_look_direction、sar_scan_heading_rad、sar_scan_origin（当前条带 start 点）。SAR 缺失/非法 look、非有限 heading/origin 必须拒绝；OFF/EO 可省略。同步旧“合法 SAR”测试构造，不把应失败的用例统一改成通过。
- [ ] CoverageController 规划后保存与 scan_ranges 一一对应的 ScanSwath 信息；反向入场和 `_replan_unflown_suffix` 偏移后仍与正确条带匹配。决策中只在扫描段发合法 SAR metadata；origin=当前swath.start，heading=swath.heading。
- [ ] executor/simulation 保持“请求 SAR”和“有效成像”可区分。executor 根据 applied_command 安装 look/heading/origin，更新实际 pose，以 origin、切线按T05公式算实际横向误差并门控 sar_imaging；Simulation._record_control_tick 不再把它强制置True或误差置0。关闭 SAR 时清掉 acquisition 元数据，原 safety 对角速度/operation 的屏蔽继续执行。
- [ ] 必须包含这个实际 sensor 几何断言：

```python
from src.env.sar_sensor import SARSensor
from src.schedule.datatypes import GridCoord
import math

def test_alternating_scan_direction_covers_same_side_of_world():
    sensor = SARSensor(swath_width_cells=1.5, near_range_cells=.25)
    east = set(sensor.compute_swath_footprint((10.5,9.75), 0.0, 'right', .8))
    west = set(sensor.compute_swath_footprint((10.5,9.75), math.pi, 'left', .8))
    assert east == west
    assert GridCoord(10,10) in east
    wrong = set(sensor.compute_swath_footprint((10.5,9.75), math.pi, 'right', .8))
    assert GridCoord(10,10) not in wrong
```

- [ ] 测试生产 factory 收到1.5而非2.0；参数变化扫幅相应变化、没有调大真实传感器；竖向、倒序、重规划 strip direction 一致；applied OFF 后 sar_imaging=False、metrics 不变；真实 heading error 非零时 frame 不伪报0。

```bash
python -m pytest tests/control/test_contracts.py tests/control/test_factory.py tests/control/test_safety.py tests/control/test_executor.py tests/control/heuristic/test_coverage_sensor_geometry.py tests/env/test_sensors_and_obstacles.py -q
```

**验收:** planner、controller、sensor 三者参数和视向一致；安全屏蔽有效。Commit: `fix: preserve SAR swath geometry through control execution`。

## T05：真实动力学闭环下的覆盖引导

**Files:** Create `src/control/heuristic/coverage_guidance.py`, `tests/control/heuristic/test_coverage_closed_loop.py`; Modify `src/control/heuristic/coverage.py`, `src/utils/coverage_planner.py`, `tests/mission/coverage_helpers.py`, `tests/utils/test_coverage_planner.py`。

**Interfaces:**

```python
@dataclass(frozen=True)
class CoverageGuidance:
    progress_cells: float
    next_index: int
    turn_rate_rad_min: float
    speed_cells_min: float
    cross_track_error_cells: float
    scan_segment_index: int | None
    is_complete: bool
```

`CoverageRouteFollower(route, *, scan_ranges, r_min)`、`update(*, position, heading_rad, speed_cells_min, dt_min, action_spec) -> CoverageGuidance`。`route` 为不可变 (c,r,heading) 序列；scan_ranges 使用 planner 原闭开端点索引语义。T06 读取 progress，T04/控制器读取 scan_segment_index/横向误差。follower还必须提供只读 `poses`、`index`（已消费路线采样点索引）、`is_complete`、`progress_cells`，兼容既有next_route_index、未飞后缀重规划和route_snapshot；index依据真实投影进度，不取lookahead点。CoverageController.act改为调用update取得guidance，不再调用旧next_command；_update_phase以guidance的扫描段/真实误差判断。

- [ ] 先运行 T01 rig 复现审计两个任务 bbox `(24,16,28,21)`、`(11,21,16,26)`，并加可控 bbox `(10,10,16,14)`，分别水平/垂直/逆序入口。记录旧 phase、航向误差、actual footprint 并保存小 CSV。现状若未复现死循环，仍必须复现至少一个错误视向/扫幅/实际漏扫红灯；不得宣称找到了未复现的追踪根因。
- [ ] 计算路线累计弧长，跳过零长度段。当前进度 s 单调，位置投影只在 `[max(0,s-v*dt), min(L,s+max(3*v*dt,r_min))]` 的连续局部区间，按最小距离选投影，再 `s=max(s,s_projected)`；候选不能跨越未经过的转弯/下一扫描行。极端安全偏离超出局部区间由 T06 重规划，不全局最近点跳行。
- [ ] 生成前视目标 `s_look=min(L,s+max(2*v*dt,r_min))` 并线性插值。转场/转弯使用 pure pursuit；在扫描直线内部优先段切线与有符号横向误差，避免追逐近航点：

```python
# Coordinates are in cells; angles use the existing project's wrap convention.
lookahead = max(2.0 * speed * dt_min, r_min)
alpha = wrap_pi(math.atan2(target_y-y, target_x-x)-heading_rad)
turn = 2.0 * speed * math.sin(alpha) / lookahead
if scan_segment_index is not None:
    desired = tangent_heading - math.atan2(0.5 * cross_track_error, speed)
    turn = wrap_pi(desired-heading_rad) / dt_min
limit = min(abs(action_spec.min_turn_rate_rad_min),
            action_spec.max_turn_rate_rad_min, speed/r_min)
turn = min(limit, max(-limit, turn))
```

`wrap_pi(x)=(x+math.pi)%(2*math.pi)-math.pi`；有符号误差 `e=-(x-x0)*sin(tangent)+(y-y0)*cos(tangent)`。真实 action spec 不对称时分别使用 min/max 两界与 ±speed/r_min 相交，而不是擅自放宽。

- [ ] `is_complete` 必须 s 距 L≤max(v*dt,0.05) 且物理位置距终点≤同容差；仅 index 到尾不足以完成。next_index 从累计弧长查找，供现有 route_snapshot 使用。
- [ ] 保留 SAR 2° 航向门槛和0.20格横向门槛；判定以执行后的真实 pose 为准；不为让测试通过扩大 tolerance。稳定进入长度至少 `max(v*dt, along_track_cells/2)`，退出同理。planner 延伸扫描线以便首末格中心进入实际成像段，延伸/连接必须重新过障碍检查。
- [ ] 用 `SARSensor.compute_swath_footprint` 栅格化预测直线的覆盖格，对 region 所有 required cells 做集合校验；不能用 `CoveragePath.covered_cells` 自我证明，因为它是规划标签。不可实现几何返回明确失败，不制造额外观测。
- [ ] 闭环测试代码核心必须包括：

```python
@pytest.mark.parametrize('bbox', [(10,10,16,14), (12,8,16,14), (24,16,28,21)])
@pytest.mark.parametrize('dt_min', [1.0, .25])
def test_real_motion_scans_every_required_cell(bbox, dt_min):
    rig = make_coverage_rig(bbox=bbox, start_pose=(6.,12.,0.), dt_min=dt_min)
    records = rig.run(max_minutes=240)
    actual = {tuple(c) for r in records for c in r['footprint']}
    required = {(c,r) for c in range(bbox[0],bbox[2]) for r in range(bbox[1],bbox[3])}
    assert required <= actual
    assert any(r['sar_imaging'] for r in records)
    assert all(r['distance_cells'] <= r['max_speed_cells_min']*dt_min+1e-8 for r in records)
    assert all(not r['obstacle_intersection'] for r in records)
```

rig 记录里 footprint 仅当 applied SAR 且真实稳定时来自真实 sensor；不能直接从 required 构造。再测试初始航向 pi、相邻平行段很近不能跳行、同位置反复update不能完成、一次安全避让后恢复、不同 dt 的面积与任务完成一致（时长允许≤5 min差异）。

```bash
python -m pytest tests/control/heuristic/test_coverage_closed_loop.py tests/control/heuristic/test_coverage.py tests/utils/test_coverage_planner.py tests/control/test_route_snapshot.py -q
```

**验收:** 全部 required cell 被真实扫到；没有 pose teleport；其它 follower 未迁移且 probe/return 原测试不退化。未通过保留 trace 修正引导，不能改为“至少扫过一格”验收。Commit: `fix: close the motion-to-SAR coverage loop`。

## T06：进展看门狗、重规划与可见诊断

**Files:** Modify `src/control/heuristic/coverage.py`, `src/control/common/{contracts,factory,coordinator}.py`, `src/vis/backend/frame_builder.py`; Create `tests/control/heuristic/test_coverage_progress.py`; Modify `tests/env/test_route_visual_frame.py`。

**Consumes:** T05 progress/scan segment、T02 timeout config。**Produces:** route_snapshot 的可选 `coverage_progress` 诊断映射（不改变 route 必填字段）；`coverage_stalled`/`task_failed` 事件。

- [ ] controller init 接受 `progress_timeout_min=10, align_timeout_min=8, max_stall_replans=2`，factory 从 CoverageConfig 传入。状态键为 task_id+generation；ControllerContext新增kw-only `generation:int=0`；coordinator._controller_context传入当前lease.generation。生产generation必须与lease一致；仅旧的直接start_task单元夹具可按0兼容。不写入不受控全局变量。
- [ ] 进展增量>0.01格刷新 last_progress_time。align 计时只在应该进入/处于扫描直线且 SAR 不稳定时累积；正常连接弯不计连续 align，route revision 不清除任务级绝对等待。
- [ ] 无进展≥10或 align≥8 发一次重规划请求。第一次未执行后缀重规划、第二次改方向、第三次失败。每次尝试之后更新下一检查的起点，但任务累计 stalled_elapsed 必须保留，用于最终失败；不允许每次 map version 更新刷计时。每次事件带 reason、task_id、generation、elapsed、attempt。
- [ ] 没有成功新路线时保持旧路径可见但 status=unavailable/明确失败，不能宣称 ready。天气版本变但未飞后缀通过安全检查时设置 `_route_status='ready'`，保持 route_revision 不变。
- [ ] 测试用可控时间的 observations：无进展10/20/30 min最多2次重规划后 task_failed；连续align8/16/24同理；正常转场60min持续增量不触发；进展后正确复位；更改map version不能无限续命；新任务重置；3次相同时间 observation 不多发事件；turn connector 不误判。
- [ ] 新 frame UAV.task_visual.coverage_progress 包含 `phase, progress_cells, remaining_route_cells, scan_segment_index, heading_error_deg, cross_track_error_cells, last_progress_min, stall_replans`；null 区分未知，不使用0顶替。

```bash
python -m pytest tests/control/heuristic/test_coverage_progress.py tests/control/heuristic/test_coverage.py tests/control/test_route_snapshot.py tests/env/test_route_visual_frame.py -q
```

**验收:** 错误路径不会永久 pending/align；能够用 frame 复盘进展。Commit: `fix: bound stalled coverage tasks and preserve valid routes`。

## T07：以本次扫描验收任务完成

**Files:** Create `src/mission/coverage_service.py`, `tests/mission/test_coverage_service.py`; Modify `src/control/heuristic/{coverage,task_flow}.py`, `src/env/simulation.py`, `src/schedule/state_manager.py`; Modify `tests/mission/test_mission_task_lifecycle.py`, `tests/control/heuristic/test_coverage.py`。

**Interfaces:** `CoverageService(fixed_mask)`；`start(task_id, generation, uav_id, bbox, at_min)`；`record(task_id, generation, cells, at_min)`；`finish(task_id, generation, at_min) -> CoverageCompletion`；`close(task_id, generation, reason)`；`progress(task_id, generation) -> CoverageTaskProgress|None`。返回 frozen dataclass：Completion 字段 `complete:bool, scanned_cells:int, required_cells:int, missing_cells:tuple, completion_pct:float`；方法不操作控制租约。

- [ ] required= bbox∩fixed_mask，任务创建时冻结；空 required 拒绝 task，不给100%；record 仅匹配 task/generation 且时间≥start，重复不累加。closed旧generation record忽略并给审计信号，不误计新任务。
- [ ] 核心红灯如下：

```python
def test_route_finish_is_not_scan_completion():
    service = CoverageService(np.ones((6,4), dtype=bool))
    service.start('S1', 1, 'U1', (0,0,6,4), at_min=10)
    scanned = tuple((c,r) for c in range(6) for r in range(4))[:11]
    service.record('S1', 1, scanned, at_min=12)
    result = service.finish('S1', 1, at_min=20)
    assert result.complete is False
    assert result.scanned_cells == 11
    assert result.required_cells == 24
    assert len(result.missing_cells) == 13
    assert result.completion_pct == pytest.approx(100*11/24)
```

- [ ] 控制器只发 `coverage_route_finished`；`HeuristicTaskFlow.EVENT_TRANSITIONS` 不直接消费此事件。Simulation `_record_control_tick` 收集到 pending completion 列表，包含 previous task/generation。`step` 在 `_update_sensors_and_detections` 后、scheduler 前 finalize；service.record 接在 T03 同一足迹后，只记录实际责任交集。
- [ ] 完整：调用原 close任务/计数/事件路径，但 completion_pct=service真实比例；排队 search_complete 给下一控制tick进入 holding。漏扫：close status=blocked、reason=coverage_incomplete，排队 task_failed，missing留给T09；不增加 completed_searches，不直接重置 I，不自主追加未经批准的任务。
- [ ] `StateManager.step` 对有 service 进展的区域读取该任务最新已归档进度；不得每分钟用历史last_scan覆盖该百分比。旧历史区域保留旧字段解释；在 frame 增加 `completion_basis='task_sar'|'legacy_observation'`。
- [ ] start 放在 assignment transaction commit 成功后，失败 batch 不留 service 状态。generation 从实际提交后的 lease 取；replace任务必须 close旧 service 后start新；rollback前不能发成功事件。
- [ ] 测试最后一格在路线结束同一tick被扫描可完成；最后一格EO不能完成；旧航次全扫过但新任务0；重派同ID新generation；天气进入不缩小required；无扫路尽只blocked；重复完成事件幂等；延迟旧事件不关闭新任务；完成一次后下一tickholding且区域不再占用。

```bash
python -m pytest tests/mission/test_coverage_service.py tests/mission/test_mission_task_lifecycle.py tests/control/heuristic/test_task_flow.py tests/control/heuristic/test_coverage.py tests/control/test_simulation_ownership.py -q
```

**验收:** 11/24例子永不显示完成或100%；sensor记录在finalize之前；漏格始终可追踪。Commit: `fix: verify search completion using task SAR footprints`。

## T08：故障撤销与资源停用

**Files:** Modify `src/control/common/coordinator.py`, `src/env/base_station.py`, `src/schedule/{datatypes,state_manager,task_allocator}.py`, `src/mission/task_catalog.py`, `src/env/simulation.py`, `src/vis/backend/frame_builder.py`, `src/vis/frontend/src/renderer/displayState.js`; Create `tests/mission/test_coverage_failure_cleanup.py`; Modify `tests/control/test_coordinator.py`。

**Produces:** coordinator `quarantine_uav(uav_id, *, current_time, reason)`；UAVState/frame `operational_status` 和 `failure_reason`；failed不属于可调度资源。

- [ ] quarantine 在原锁/atomic 边界中停止当前controller、撤销lease增加generation、清pending/current controller/event队列/保存的coverage和last command，operation IDLE只表示没有控制命令，**不表示可用飞机**。使用已有 ownership.release_to_system；无controller重复调用幂等。不得安装一个未经安全检查的holding航迹。
- [ ] `_enter_emergency_failure` 先保存旧task/lease，再 `_close_mission_task(...status='blocked', reason=...)`、service.close，解除 contact/probe/track和search绑定，清除 `_return_base_by_uav`/`_holding_base_by_uav` 中该机预留，并调用新增 `BaseStation.remove_uav(uav_id) -> bool` 从refueling queue/hangar移除（不增加refuel_count），quarantine并设置sm UAV operational_status=failed，关SAR并清footprint，发布一次failure事件。无实际安全动作时保留最后位置且记录episode invalid，不能说已安全返航。
- [ ] 单一helper `StateManager.is_uav_operational(uav_id)` 供 get_available_uavs、TaskCatalog._resource_ids、TaskAllocator._mission_resources 使用；同时在apply_assignment_batch验证阶段拒绝failed，避免过期snapshot重派故障机。
- [ ] `CandidateExtractor` 的occupied只读取有有效绑定、未failed的active search记录；stale/unassigned不锁区。
- [ ] 显示函数优先failed→“故障停用”，保持原状态颜色兼容但不再叫正在转场；新字段缺失按旧逻辑。
- [ ] 测试UAV-5型NoSafeRecoveryPath：任务、service、region、contact、probe、base reservation、lease全部清理；剩余机可以合法接手原区；重复异常只记一次；旧generation completion不动新接手机；failed不出现在available/prompt edge；健康机controller/fuel不受影响。

```bash
python -m pytest tests/mission/test_coverage_failure_cleanup.py tests/control/test_coordinator.py tests/mission/test_mission_task_lifecycle.py tests/mission/test_contact_release.py tests/control/test_ownership.py -q
```

**验收:** 冻结故障机不占区域、不作为可用机参与保底分子；审计能看见故障。Commit: `fix: release failed mission bindings and quarantine UAVs`。

## T09：全域责任与稳定候选地理身份

**Files:** Create `src/mission/coverage_policy.py`, `tests/mission/test_coverage_policy.py`; Modify `src/schedule/candidate_extractor.py`, `src/mission/task_catalog.py`; Modify `tests/schedule/test_candidate_extractor.py`, `tests/mission/test_task_catalog.py`。

**Interfaces:** `CoveragePolicy(fixed_mask, *, primary_window_min=60)`；`classify(*, now_min, last_sar, feasible_mask, assigned_mask, legal_candidate_mask) -> dict[str,np.ndarray]`；`rank_search_candidates(candidates, *, now_min, last_sar, estimated_minutes) -> tuple`。后者 estimated_minutes 为 task_id→正浮点 mapping，缺路线时用bbox覆盖长度/实际速度的保守估计，不虚构已算好的精确成本。

- [ ] 先写分区穷尽测试，核心断言：

```python
classes = policy.classify(now_min=120, last_sar=last,
    feasible_mask=feasible, assigned_mask=assigned, legal_candidate_mask=legal)
stack = np.stack(list(classes.values())).astype(int)
assert np.array_equal(stack.sum(axis=0), fixed.astype(int))
assert classes['deferred_weather'][storm_cell]
assert classes['waiting_due'][waiting_cell]
assert classes['fresh'][fresh_cell]
```

- [ ] 以固定A按优先次序分配：fresh=last>now-W；remaining∩~F→weather；remaining∩有效assigned→assigned_due；remaining∩~legal_candidate_mask→geometry；其余waiting。矩阵必须都copy，shape、时间校验明确。
- [ ] rank key 为 `(0 if unseen_count>0 else 1, oldest_due_time, -due_count/max(estimated_minutes,1e-6), bbox)`。从未扫描的oldest_due_time=episode开始0；过期格为其last_sar；不通过new task ID改变时间。仅判断真实SAR，不用已被EO刷新的I作为覆盖新鲜度。
- [ ] 搜索task_id固定 `search:{c0}:{r0}:{c1}:{r1}`，不包含枚举序号。修改原snapshot/tests中的ID断言；同几何重派以lease generation区分，历史TaskRecord关闭再创建新的本轮record，日志保留过去记录。不能因记录里存在同ID completed而永久禁止重访。
- [ ] 候选枚举增加 fragment 第二遍：`unserved = due & F & ~occupied & ~union_of_regular_candidates`；对每个unserved格枚举包含该格的1–19格bbox，仍符合aspect_ratio_max，按面积、bbox排序，使用原 `_has_turning_clearance` 和后续完整route验证；仅保留能为unserved增加服务范围的框，稳定ID加前缀 `fragment:`，TaskCandidate.kind仍search。去重通过bbox。若bbox跨不可飞格或无转弯空间明确排除。
- [ ] 几何暂缓每次map变化或候选池重建重新检验，永久等待时间不清零；小片无法安全规划仍在A中，输出理由，不能无条件把1格任务标可行。
- [ ] 测试全未扫描海域进入责任；I=1但SAR=-inf仍due；任意天气/旧completed/failed不会吞责任；同bbox风暴前后ID相同；同bbox第二代任务可出现；1/7/19格残片都可生成或明确geometry原因；20格走常规；碎片不得穿障碍；海域中有洞时domain计数准确。

```bash
python -m pytest tests/mission/test_coverage_policy.py tests/schedule/test_candidate_extractor.py tests/mission/test_task_catalog.py tests/mission/test_intent_candidates.py -q
```

**验收:** 每格都可解释；ID稳定；不将未入prompt当不可达。Commit: `feat: track whole-sea coverage obligations and residual cells`。

## T10：只选择一次候选窗口，并提前保证公平

**Files:** Modify `src/schedule/task_allocator.py`, `src/mission/{contracts,prompt_window,mission_scheduler}.py`; Create `tests/mission/test_coverage_prompt_window.py`; Modify `tests/mission/test_prompt_window.py`。

**Interfaces:** frozen `MissionSnapshot` 添加默认 kw-only `prompt_task_ids:tuple[str,...]=()`、`prompt_sources:tuple[tuple[str,str],...]=()`；新增 frozen `CoverageCandidateWindow(tasks:tuple[TaskCandidate,...], representative_task_ids:tuple[str,...], sources:tuple[tuple[str,str],...])` 与 `CoveragePolicy.select_window(ranked_candidates, *, ordinary_reserve, capacity, now_min) -> CoverageCandidateWindow`。ranked_candidates直接消费T09责任排序结果，representative_task_ids显式标出互不重叠的普通代表，供T11取用；内部保留未检验地理键FIFO；旧snapshot空prompt_task_ids只允许测试兼容路径，不改变生产新路径。

- [ ] 移除 `_mission_edges` 中自己的top utility截断。`build_mission_snapshot` 明确顺序：全池→责任排序→候选窗口→对应route/edges→无边补位→冻结；每轮最多120候选完整几何检查，初始窗口≤40，普通至少8（存在足量时）。剩余urgent/geography/fair沿用并去重。
- [ ] reserved ordinary先构造不重叠代表框：按T09顺序，加入时与已选ordinary代表bbox不相交；可额外放重叠备选于非保底槽位。若凑不齐8个非重叠框，允许普通候选其余槽位放重叠备选，但T11只能使用明确标为代表的子集。
- [ ] 没边ordinary补下一个；满120停止，保留cursor；未检验=geometry_budget_deferred、仍waiting，不是deferred_geometry。map change使几何cache过期，但不重置FIFO地理年龄。每个geometry检查耗时/计数可审计。
- [ ] `_prompt_payload` 严格按snapshot.prompt_task_ids取数据，禁止再次调用PromptWindow.select重新决定；`_filter_prompt_candidates` 不能把保底代表框悄悄删掉，geometry校验前置。metadata只记录真正送到模型的ID。普通完整池保留snapshot.candidates供审计，但asdict不要将它全塞Prompt。
- [ ] 行为测试：40槽、45个urgent+100普通仍显示≥8普通；1000个高utility局部任务不能挤掉最老远端格；连续5轮中普通地理代表轮换且未检验队列推进；拿到edge的候选与PromptIDs一致；无边补位不超120；新候选不会插队重置老格；真实候选2项不要求8。

```python
def test_urgent_load_does_not_remove_ordinary_reserve(window_fixture):
    snapshot, payload, geometry_calls = window_fixture(urgent=45, ordinary=100)
    ids = set(snapshot.prompt_task_ids)
    shown = payload['snapshot']['candidates']
    assert {t['task_id'] for t in shown} == ids
    assert len(shown) <= 40
    assert sum(t['kind']=='search' for t in shown) >= 8
    assert geometry_calls <= 120
```

`window_fixture` 是本测试文件创建的TaskCandidate/FeasibleEdge合成夹具，几何回调可控计数；不能让真实LLM参与这个单元测试。

```bash
python -m pytest tests/mission/test_coverage_prompt_window.py tests/mission/test_prompt_window.py tests/mission/test_mission_scheduler.py tests/schedule/test_task_allocator.py -q
```

**验收:** 提示公平发生在边预筛前，不在被截断集合上做表面公平。Commit: `fix: reserve coverage candidates before route filtering`。

## T11：可行搜索保底与 LLM 选择校验

**Files:** Modify `src/mission/{coverage_policy,contracts,mission_scheduler}.py`, `src/schedule/task_allocator.py`, `src/mission/prompts/mission_scheduler.txt`; Create `tests/mission/test_coverage_reservation.py`。

**Interfaces:** frozen `CoverageConstraint` 字段 `desired_search_count:int, active_search_count:int, required_new_search_count:int, representative_task_ids:tuple[str,...], must_service_task_ids:tuple[str,...], infeasible_reason:str|None`；MissionSnapshot kw-only `coverage_constraint:CoverageConstraint|None=None`。独立 `build_coverage_constraint(*, healthy_count, active_search_count, available_ids, representatives, edges, fraction) -> CoverageConstraint`。

- [ ] 计算desired=ceil(healthy_count*fraction)，active只算健康、无watchdog失败、绑定一致的普通coverage（search/direction_search/investigation都占搜索资源，但pending故障不算）。安全/返航/加油/保护probe/track不算available。
- [ ] 在不重叠representatives与available之间用简单增广路径最大二分匹配算capacity，不用“候选数”和“空闲机数”最小值代替，因为可行边分布可能不同。

```python
def maximum_search_slots(task_ids, available_ids, edges):
    allowed = set(available_ids)
    adjacency = {task: sorted({e.uav_id for e in edges
                               if e.task_id==task and e.uav_id in allowed})
                 for task in task_ids}
    owner = {}
    def augment(task, seen):
        for uav in adjacency[task]:
            if uav in seen:
                continue
            seen.add(uav)
            if uav not in owner or augment(owner[uav], seen):
                owner[uav] = task
                return True
        return False
    return sum(augment(task, set()) for task in sorted(task_ids))
```

- [ ] required=min(max(desired-active,0),capacity)。如果不足，明确reason=`insufficient_available_resources`或`no_feasible_search_edges`，不是伪造达标；Prompt必须含精简constraint，required>0时，must_service_task_ids设为有可行边的最老责任代表（单元素）；required=0时为空。模型须包含该代表，其余可选择任意足量可联合匹配子集。不得让“进过Prompt”冒充真正服务公平。
- [ ] validate_selection在已有几何/资源校验后检查保底；少选返回 `coverage_floor_not_met`，漏掉最老代表返回 `coverage_oldest_not_selected`，沿已有纠错机制回模型。还要校验整体选集可匹配，避免选出的probe消耗了唯一可做search的机。pair_selected_tasks复验，apply_assignment_batch再次按当前healthy及generation拒绝过期结果。
- [ ] 抢占普通search后不得使可实现的active count低于floor；不会为了保底抢占保护probe/track。恢复正常资源后新的snapshot提升required。不得自动往selected_task_ids追加任务。
- [ ] 用例：10健康、0active、10可用和6合法代表→required4；2active→required2；9受保护、1可用→required1+不足原因；全部受保护→required0且允许合法defer；所有候选只能同一机→capacity1；挑4个区域但仅3机可匹配→拒绝；floor满足的probe可执行；低于floor的preempt拒绝；选满4个却漏最老代表拒绝；已分配责任格不重复当作最老待派；模型失败不发assignment。

```bash
python -m pytest tests/mission/test_coverage_reservation.py tests/mission/test_mission_scheduler.py tests/mission/test_intent_candidates.py tests/mission/test_handoff.py tests/mission/test_mission_task_lifecycle.py -q
```

**验收:** 有能力时保留搜索、无能力时明确解释，不牺牲合法返航。Commit: `feat: enforce feasible search capacity in mission selection`。

## T12：压缩模型输入并记录真实时延

**Files:** Modify `src/mission/{mission_scheduler,llm_gateway}.py`, `src/schedule/task_allocator.py`, `src/mission/prompts/mission_scheduler.txt`; Create `tests/mission/test_coverage_decision_budget.py`; Modify `tests/mission/test_llm_gateway.py`。

**Produces:** interaction.timing内 snapshot_seconds、prompt_seconds、llm_seconds、validation_seconds、matching_seconds、total_seconds、prompt_bytes；总预算仍来自information_update配置。

- [ ] 不从 `_jsonable(snapshot)` 全量contacts开始再删；明确白名单序列化当前Prompt任务、resources、必要active任务、intent、contacts摘要；保留coverage_constraint及must_service_task_ids、prompt_sources和全部校验所需generation。contact最多20，历史keypoints最多12；复用 `trajectory_features.select_keypoints`，latest位置/类别证据/可行性字段保留。context max不存在的字段不能猜造。
- [ ] reviewer_summary按Unicode字符截到1200；LLM notes最大160字符（validator反馈）；decision_maker调用显式max_tokens=1536；complete candidate set留在本地snapshot，不发模型。不可删除selection验证所需的task/generation/edge信息。
- [ ] `time.perf_counter`分别包围snapshot、prompt、gateway、validation、matching；绝对deadline在冻结snapshot时建立（保持既有定义），preparation单列；总预算检查共享同一个deadline，不给每次纠错新30s。把gateway内回调验证时长从llm_total中扣除或命名为transport_and_validation并另记，不能重复求和。
- [ ] 采用注入时钟的fake transport，不sleep 30s。时序例：prompt0.2s、模型29.0s、校验0.2s、matching0.1s→成功，总29.5；transport达到29.5预算后无后处理时间→timeout；matching跨过30s即失败。精确临界按现有 `_deadline_expired` 一致处理。
- [ ] 1000 contacts/600 samples的构造仅用于输入压缩压力测试：输出≤20contacts且每个≤12样本；候选≤40；原snapshot未改变；没有hidden class/scenario inventory。assert prompt_bytes明显低于全量并设100 KiB保护上限；超过上限拒绝 `prompt_budget_exceeded`并定位字段，不无提示截断关键合法边。
- [ ] 现有selection_interaction的errors/success必须反映最终matching/校验结果，而非仅模型返回JSON成功；记录 `failure_stage` 取 preparation/prompt/transport/validation/matching/apply。

```bash
python -m pytest tests/mission/test_coverage_decision_budget.py tests/mission/test_llm_gateway.py tests/mission/test_mission_scheduler.py tests/mission/test_maritime_acceptance.py -q
```

**验收:** fixture能证明deadline行为与时间分项一致；不把离线速度说成线上模型速度。Commit: `perf: bound mission prompts and measure decision stages`。

## T13：连续模型失败暂停与按角色重试

**Files:** Modify `src/env/simulation.py`, `src/schedule/task_allocator.py`, `src/vis/backend/server.py`（仅若现有runtime API限定角色）; Create `tests/mission/test_coverage_model_failure.py`; Modify `tests/env/test_server_runtime.py`, `tests/mission/test_failure_paths.py`。

**Consumes:** T02 max failures、T12最终failure_category/stage。**Produces:** paused_model/blocked_role=decision_maker，保持既有red方协议。

- [ ] engine以heavy决策最终成功/失败更新连续计数；成功batch或合法无任务决定清零，`none/light`不误清零。每一决策只记一次失败，不按广播帧数累加。第一次/第二次继续旧任务且下一仿真分钟可重试；第三次设置paused，不自动新调度。
- [ ] gateway已有http_401/402/403/configuration类别，原样传播到engine；这些直接暂停（http400按请求错误记录并走上限，不无限重试）。不根据包含“error”的普通字符串猜额度。调用失败reason日志先脱敏。
- [ ] `retry_blocked_decision`按blocked_role分派：red保持原逻辑；decision_maker在同clock.time新snapshot，仅调用任务调度一次，不运行step的ship/control/sensor更新；成功commit后清计数、running；失败保持paused。实现时给 `TaskAllocator.mission_step` 加 kw-only `force_heavy:bool=False`；人工重试用True建立 `TriggerDecision("heavy", "operator_retry")` 跳过正常触发冷却且只执行一次，其余正常调用仍False。不得仅重试时调用check返回none便伪报成功。再次重试不会复用过期batch/generation。
- [ ] runtime retry/abort仍通过现有命令队列；paused step调用只能处理pending命令，不能tick。main日志循环当前已修复遇pause停止，应增加回归，禁止500份重复状态继续输出。
- [ ] 明确测试：两次timeout旧机继续；第三次暂停；402首轮暂停；暂停100次step clock、fuel、coverage都相同且gateway调用次数不增；operator retry成功clock不变，下一正常step恰加dt；retry失败不清原因；abort不会重调；red失效仍暂停；旧有效red计划按原契约可执行；从不fallback规则分配。

```bash
python -m pytest tests/mission/test_coverage_model_failure.py tests/mission/test_failure_paths.py tests/env/test_server_runtime.py tests/mission/test_red_commander.py tests/mission/test_simulation_flow.py -q
```

**验收:** 外部故障不会伪装为长期运行、无限重试或正常覆盖；真实运行若BLOCKED必须显式交付。Commit: `fix: pause repeated mission model failures and retry by role`。

## T14：右侧持续覆盖面板与实时刷新

**Files:** Create `src/vis/frontend/src/components/CoveragePanel.jsx`, `src/vis/frontend/tests/coverage-metrics.spec.js`; Modify `src/vis/frontend/src/components/RightSidebar.jsx`, `src/vis/frontend/src/App.jsx`, `src/vis/frontend/src/App.css`; Create `src/vis/frontend/tests/helpers/frameSocket.js`（提取现有 mock，不改变生产WS协议）。

**Consumes:** frame.coverage_metrics、frame.runtime_status/sim_time_min、App现有live.status与mode。**Produces:** `CoveragePanel({frame, connectionStatus, readOnly})`。App→RightSidebar新增connectionStatus，取live模式的live.status、replay模式现有replayConnectionStatus。

- [ ] 在任务概览后、船舶编辑前插入面板；原 Metric“海域覆盖”改名“累计观测覆盖”，title说明含光电历史扫描。其它船舶编辑/Intent/Contact位置顺序保留。
- [ ] 下面是状态选择的核心，实际组件补齐设计 §9 规定的文本、按钮、进度条和面积：

```jsx
const [windowMin, setWindowMin] = useState(60);
useEffect(() => setWindowMin(60), [frame?.episode_id]);
const metrics = frame?.coverage_metrics;
const supported = metrics?.schema_version === 'persistent-coverage/v1';
const windowData = supported && Array.isArray(metrics.windows)
  ? metrics.windows.find((item) => item.minutes === windowMin)
  : null;
const percent = windowData?.coverage_pct;
const percentText = Number.isFinite(percent) ? `${percent.toFixed(2)}%` : '—';
```

null metrics→“该回放未记录持续覆盖指标”（readOnly）或“等待覆盖指标”；unknown schema→“不支持的指标版本”；no_searchable_area→“无可搜索海域”；partial window→“窗口积累中”；status必须由frame/backend字段得出，不从百分比是否0推测。
- [ ] 按钮 aria-label=`最近 30 分钟`/60/120，aria-pressed；主值 `data-testid='coverage-primary-value'`；section aria-label=持续搜索覆盖；有值时progressbar aria-valuemin0/max100/now=percent且视觉clamp到[0,100]。不将null变为0。取窗口变化时unseen不变，overdue_seen应使用`cumulative_pct-windowData.coverage_pct`，不能一直显示primary60的逾期数。
- [ ] 不设任何timer计算覆盖，不从info_matrix推测，不单独API请求；实时和回放都直接读取当前frame。App的连接状态用于“连接中断，非实时”，仅live显示；paused_model显示指标冻结时刻；readOnly显示“回放数据”。时刻来自metrics.as_of_min而不是墙钟。
- [ ] CSS复用sidebar-section/字体/边框，3按钮一行，主值tabular-nums，面积可换行，min-width:0；桌面304px和移动390px viewport均不横溢。字体≥12px，焦点可见，window切换无需鼠标。
- [ ] 将现有mixed-maritime.spec.js中frameFixture/installFrameSocket提取为测试helper，导出且同时改原测试import，保留 `window.__pushFrame`。允许coverage新测试直接构造最小有效frame，但不要把200行同样fixture复制三遍。
- [ ] 必须实现下面实时更新用例（fixture里的覆盖元数据counts/area须和pct一致，用固定100格域简化）：

```javascript
test('coverage follows pushed frames without reload', async ({ page }) => {
  const initial = coverageFrame({ minute: 120, cells60: 8, domainCells: 100 });
  await installFrameSocket(page, initial);
  await page.goto('/');
  const panel = page.getByRole('region', { name: '持续搜索覆盖' });
  await expect(panel.getByTestId('coverage-primary-value')).toHaveText('8.00%');
  const updated = coverageFrame({ minute: 121, cells60: 12, domainCells: 100 });
  await page.evaluate((frame) => window.__pushFrame(frame), updated);
  await expect(panel.getByTestId('coverage-primary-value')).toHaveText('12.00%');
  await panel.getByRole('button', { name: '最近 30 分钟' }).click();
  await expect(panel.getByRole('button', { name: '最近 30 分钟' })).toHaveAttribute('aria-pressed','true');
});
```

`coverageFrame` 同文件实现为 `frameFixture` 上覆盖fields：episode、sim time及3windows，默认30=min(cells60,5)、120=max(cells60,20)、cumulative=max(20,cells60)，面积=counts*100。测试父region需组件显式 role=region或命名section语义。
- [ ] 额外测试：0.00%正常值、null和旧记录缺字段、unsupported schema、空海域、未满窗口、同time重复推帧、paused真实3.40%不变化、断线保留旧值加状态、重连更新、新episode归零且按钮回60、replay seek向前/向后正确替换不累加、慢frame/跳帧收到最新帧、窗口切换时面积/逾期一致、390px/键盘焦点、故障UAV状态显示。控制旧replay API mock复用现有fixture机制，禁止另起全新state store。

```bash
npm run build
npx playwright test tests/coverage-metrics.spec.js tests/mixed-maritime.spec.js
```

**验收:** 右侧动态显示与frame逐值一致，桌面及移动截图人工检查；没有NaN和“缺失=0”。Commit: `feat: show live rolling SAR coverage in the sidebar`。

## T15：独立复算、长期场景和评估输出

**Files:** Create `scripts/evaluate_persistent_coverage.py`, `tests/mission/test_persistent_coverage_evaluation.py`; Modify `scripts/persistent_coverage_scenarios.py`（保留T03场景配置及CoverageFixtureGateway源码，新增采样/检查入口，不改模型选择策略）; Modify `src/mission/outcome_evaluator.py`, `src/env/simulation.py`；可复用 `scripts/replay_restoration_scenarios.py` 的纯capture_frame、hash/manifest辅助，但不使用它的人为改变位姿的hooks。

**Interfaces/CLI:**

```bash
python scripts/evaluate_persistent_coverage.py run --scenario coverage-open-water --transport fixture --seeds 42 43 44 45 46 --steps 720 --output-dir outputs/evaluations/persistent/open-water
python scripts/evaluate_persistent_coverage.py run --scenario coverage-mixed-weather --transport fixture --seeds 42 43 44 45 46 --steps 720 --output-dir outputs/evaluations/persistent/mixed
python scripts/evaluate_persistent_coverage.py check-log --log outputs/evaluations/persistent/mixed/seed-42/frames.jsonl --output-dir outputs/evaluations/persistent/check-mixed-42
python scripts/evaluate_persistent_coverage.py compare --baseline-dir outputs/evaluations/persistent/baseline-mixed --candidate-dir outputs/evaluations/persistent/mixed --output-dir outputs/evaluations/persistent/comparison
python scripts/evaluate_persistent_coverage.py live-budget --scenario coverage-mixed-weather --steps 720 --seeds 42 43 44 45 46
```

输出布局固定为输出目录下 `manifest.json`、`summary.json`，每seed在 `seed-42` 等子目录保存 `manifest.json`、`frames.jsonl`、`metrics.csv`、`outcome.json`。check-log在log同目录读取manifest，原生log缺配置/geometry标BLOCKED；legacy另用明确的reconstructed路径，不猜配置。

`run/check-log/compare/live-budget`由argparse子命令实现，拒绝不认识flag、已有输出manifest、非正steps、重复/空seeds。exit0=检查PASS，1=验收FAIL，2=基础设施/外部模型BLOCKED或数据不全；`live-budget`只输出预算，不调用模型。

- [ ] 场景定义固定：open-water=当前物理/10机/2基地，vessel total=0、island min/max=0、storm min/max=0；mixed-weather=当前默认ship/environment/uav/sensor及新coverage配置，保证≥10机，不减船/关雷暴来过门槛。seeds固定42–46。
- [ ] 测试模型gateway只替代模型边界：decision_maker从公开candidate/edges选择满足floor且非重叠可匹配的集合，先floor代表再剩余utility；red/contact/reviewer沿用确定性fixture响应且合法验证，不读取hidden truth、不直接assign controller、不改坐标。在manifest明确“fixture不验证真实LLM质量”。
- [ ] run逐次engine.step，每次clock推进才record采样；保存首帧0、实际每tick frame和一次终态；遇paused/finished提前终止并标明原因，不能重复填充到720。输出全量参数（包括MissionConfig的activity/evasion/information_update companions和coverage）、git SHA、dirty diff hash、seed、model binding（不含key）、transport、clock dt、实际终止时间、schema、平台版本。
- [ ] EvaluationTick添加kw-only可选 `persistent_coverage:dict|None=None`；EpisodeOutcome增加默认dict字段 `persistent_coverage`，维持原required构造兼容。observe只对严格推进时刻积分，重复同时间不累加；未提供的legacy tick输出availability=false，不能0。既有score先保留并另给coverage objective结果，strategy_memory不把缺新指标当新版赢。
- [ ] check-log不调用生产CoverageMetrics：用T01算法思路独立构建last_sar，按sar_scan事件去重。重复发布相同事件只记一次；同episode/uav/time相异payload冲突报错。对每个frame逐窗口核对counts/area/pct（count精确、pct容差1e-9）；同时从实际pose/heading/look/near/swath/along_track按点中心投影**独立重算**footprint，与sar_scan对应，不能只比较两份同源汇总。episode manifest必须记录T04每机固定execution config（swath、near、along_track、cell_size、R_min、heading tolerance）；UAV帧补充实际sar_scan_heading_rad/sar_scan_origin供诊断。check-log按manifest几何和帧实际heading/look重算，变化须新配置版本。
- [ ] 旧日志check-log若没有原生sar_scan/coverage_metrics进入明确 `legacy_reconstructed` 模式：按审计info==1且真实SAR footprint重建，只提供描述性结果，不作新生产契约PASS。不缺时间/矩阵则不可擅自返回全0；注明无法判断任务generation。
- [ ] 计算W=30/60/120 fixed/dynamic，完整窗口的采样均值、时间均值、P5、最小、最长0覆盖段；首次覆盖/到期格刷新面积、有效SAR UAV·min、可用机时、idle、deferred原因、决策成功率/耗时、faults。时间均值采用左端点持有到下个已知仿真tick；partial区间从W切入，最后无后续tick不外推。所有门槛统一采用这个时间积分定义。
- [ ] compare仅在相同seed/config/scenario/transport/窗口口径时配对；不同code SHA允许，config hash不同要求显式报告差异、不生成“受控提升率PASS”。既有两历史log只是reference，不冒充paired baseline。比较已有classification/discovery/handoff的差值与分母，缺分母为N/A且不能算通过。
- [ ] 自动化unit包括：2×2事件oracle随机序列100组；重复/乱序 frame、缺tick、reset、非法将来事件、SAR与EO、固定域变化、无domain、窗口未满、跨dt积分；篡改summary+1格能被抓；篡改pose/footprint能被抓；不足720分钟不会达标；CLI不覆盖产物；live-budget不调用transport；compare拒绝fixture/live混配。

```bash
python -m pytest tests/mission/test_persistent_coverage_evaluation.py tests/mission/test_outcome_evaluator.py tests/mission/test_evaluation_cli.py tests/mission/test_strategy_validation.py -q
```

**验收:** 原生指标可由原始扫描与几何独立复算；结果包含PASS/FAIL/BLOCKED而非只有一条平均值。Commit: `test: add persistent coverage replay and endurance gates`。

## T16：整体验证和交付

**Files:** Update `docs/validation/persistent-coverage-execution.md`, `docs/VALIDATION.md`（仅补充新口径和真实结果链接，不覆盖旧结果）；产物存outputs/evaluations/persistent/，不提交大型JSONL。

- [ ] 按[验证矩阵](../../validation/2026-09-17-persistent-coverage-validation.md)核对每个C/G项已有test或实测结果。所有单测都绿后只做一次全套回归：

```bash
python -m pytest tests -q
```

- [ ] 前端构建和全套浏览器回归（cwd=src/vis/frontend）：

```bash
npm run build
npx playwright test
```

- [ ] 先运行单seed42、180min的open-water和mixed smoke；确认无故障与可复算后，再跑T15列出的5seeds×720min两组fixture。记录运行时长/失败点，不能超时后留下后台继续无限请求。
- [ ] 独立check每个新log，再compareT03保存的同配置baseline；该telemetry-only基线已有原生扫描，不需重新运行旧代码。若外部另给的基线无法产原生metrics可用独立legacy扫描重建，但必须保留measurement_mode差异，若没有足够原始数据则该baseline对照BLOCKED，不能捏造。绝对工程门槛仍可单独判定。
- [ ] 使用真实后端新生成log在回放界面核对t=60/120/177/240/480/720，截图记录30/60/120切换。再使用真实生产frame的WebSocket推送核对数值更新；浏览器mock通过不替代这一步。保留网络payload、截图、console errors检查、每个时刻CSV行号。
- [ ] 真实模型验证先运行live-budget列明token/calls预算，按用户对模型调用的既有授权执行。若当前授权范围只有文档/离线实施，不自行消耗长程live额度；保留live门禁为NOT_RUN并明确原因。获得运行范围后运行相同mixed配置、5seeds×720min，不能改sensor/机数提高成绩；API限额导致暂停为BLOCKED，不是算法PASS。
- [ ] 最终执行记录必须列：HEAD/dirty状态、配置hash、执行任务编号、测试命令与真实退出码、测量来源、每seed实际仿真分钟、coverage各窗口指标、任务漏扫/fault/超时、UI截图、未完成门禁。文档设计指标与实测结果分列。
- [ ] 自审：`rg -n 'coverage_pct|completion_pct.*100|sar_heading_error_deg.*0|sar_look_direction.*right' src` 逐处解释仍存在的旧兼容赋值，不能只因为找到0/right就删除合法初始化。检查未引入真正安全绕过或真值调度。

**验收:** C01–C24契约、G01–G05功能/fixture门禁通过方可称“离线实施完成”；G06真实模型通过才可称“在线覆盖目标验证通过”。若G03/G04未过，继续定位当前任务，不能只交付UI后关闭问题。Commit: `docs: record persistent coverage verification results`。

## 2. 交接检查表

- [ ] 所有新增公开符号签名在调用处一致，额外dataclass字段有向后兼容默认。
- [ ] fixed主分母、SAR来源、窗口边界、episode键在backend/CSV/UI一致。
- [ ] 候选空间、真正分配、实际成像、完成验收分别有证据，不能互代。
- [ ] 故障、deadline、模型暂停、历史缺指标都不是“覆盖0且正常运行”。
- [ ] 与85%识别/接力既有目标的回归被记录，不能为了覆盖占满10架导致其它任务失效。
- [ ] 真正验收结果以产物为准；本计划本身不是通过证明。
