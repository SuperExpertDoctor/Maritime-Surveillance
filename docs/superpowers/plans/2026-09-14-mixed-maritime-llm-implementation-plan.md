# Mixed Maritime LLM Implementation Plan

> 术语已按 2026-09-16 统一：分类使用 I 类船舶/II 类船舶，运行时值使用 `type_i`/`type_ii`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> 本方案供 Luna Max 执行；若执行环境无上述技能，遵守本文任务顺序、接口契约和验证门槛即可，不因工具名称不同改设计。**当前状态：待用户审阅，禁止开始代码实施。** 用户批准后在独立分支或 worktree 执行；此文不是要求现在执行命令。

**Goal:** 实现 N 艘混合船舶、全局 AIS、对方 LLM 参数化逃逸、我方历史行为核查与统一调度、人工重点区和受验证策略记忆。

**Architecture:** 保持单仿真写线程，以显式快照和 schema 连接模型。物理真值、我方观测、对方决策和评估隔离；LLM 选择受约束的动作，现有 ControlCoordinator 与 Hybrid A* 执行。把新状态和逻辑放入小模块，SimulationEngine 只做编排，不继续堆叠身份、意图和经验规则。

**Tech Stack:** 现有 Python/dataclasses/numpy/scipy/PyYAML/OpenAI-compatible SDK、FastAPI/Pydantic、React/Canvas、pytest、Playwright。不新增 agent 框架、数据库或模型训练框架。

## Global Constraints

- 设计唯一来源：[设计文档](../specs/2026-09-14-mixed-maritime-llm-design.md)，以下简称 D；特别遵守 D §4–§11 数据字段、单位、枚举、默认值和错误行为。
- 用户审阅通过前只允许修改文档。全部下列 checkbox 是未来实施工作，不代表已完成。
- 真实运行必须调用真实 LLM；unit/contract 测试可使用标记 fixture，但不得计为模型或自演进成功。
- 我方不得读取真实身份、未观测静默目标、对方逃逸状态和参数；评估器可读真值但不回灌本回合。
- 控制任务安装、释放、抢占只能通过 ControlCoordinator/ownership；不得直接设置 UAV waypoint/status 绕开控制契约。
- 无模型合法输出不得生成替代逃逸、身份终判或新全局任务；失败行为按角色分别处理。
- 所有阈值使用仿真分钟、连续格坐标；角度 API 为度、控制器为弧度；bbox 半开；N 与 UAV 容量不写死。
- 所有配置和示例值是首版提案，用户审阅修改后先同步 D，再实施。
- 提交只包含任务文件，不覆盖用户改动、不删除历史 outputs；每个 task 验证后提交，提交 message 见各 task。

---

## 0. 执行方法、依赖与审查门

先阅读 D 全文（包括 §14 跨任务契约）、`src/control/interface.md` 和现有测试。各 task 内先添加契约/行为测试，确认失败是目标功能缺失，再实现并运行指定测试。下列代码块是接口和关键控制逻辑规范；与 D 中完整字段一起使用，不是允许用 pass/恒定返回值替代业务实现。现有文件行号会变化，用文中函数符号定位。命名fixture按D §14.5随对应task实现，测试helper不能直接写最终身份或跳过模型/控制接口。

每个 task 的交付记录写入 `docs/implementation/mixed-maritime-progress.md`：task ID、commit、修改文件、测试命令、通过/失败/跳过数、实际 API 是否调用、未决问题。未通过门槛不进入依赖任务；不要自行缩小验收。

| 阶段 | Tasks | 可审查交付 |
|---|---|---|
| A 数据与船舶 | T01–T05 | 严格契约、N 船场景、模型网关、参数逃逸及合法导航 |
| B 观测与核查 | T06–T09 | 全局 AIS、接触历史、证据研判、递进控制 |
| C 统一调度与意图 | T10–T13 | 意图状态、搜索/核查/跟踪统一选择、主循环闭环、API |
| D 可视化与追溯 | T14–T15 | 人工框选、观测 UI、回放及指标日志 |
| E 自演进与验收 | T16–T18 | 候选经验、离线验证/版本管理、全场景验收与交付 |

依赖图：T01→T02,T03,T06,T10；T02+T03→T04；T02+T04→T05；T06→T07；T03+T07→T08；T07+T08→T09；T03+T06+T10→T11；T05+T08+T09+T11→T12；T10+T12→T13；T13→T14；T12→T15；T03+T15→T16→T17；全部→T18。T02/T03/T06/T10 可独立实现，但 ConfigLoader 和 SimulationEngine 修改需要顺序集成，不能多个 worker 同时改。

### 0.1 一次性准备（批准后）

- [ ] 记录 `git status --short`、`git rev-parse HEAD`，确认工作目录和用户改动；建立实施分支，不自动 merge。
- [ ] `python -m pytest --collect-only -q` 记录基线；当前部分集成测试实例化真实模型配置，缺少 key 的测试应明确标记，不能输出环境变量值。
- [ ] 在 `src/vis/frontend` 执行 `npm run build`，记录前端基线。没有依赖时使用 lockfile 安装并记录，而不升级依赖版本。
- [ ] 审查 `AGENTS.md`（如果实施时存在）、控制接口、下面的文件地图；所有与 D 不同的需求先写差异，不自作主张实现第三种行为。

### 0.2 文件地图

新增 `src/mission/` 模块：

| 文件 | 唯一职责 |
|---|---|
| `contracts.py` | D 数据类型与快照，不导入 Ship/SimulationEngine |
| `config.py` | 新配置 dataclass 与验证 |
| `llm_gateway.py` | 四角色请求、严格 JSON、重试和调用记录 |
| `red_commander.py` | 对方快照、距离门控、参数校验与集中调用 |
| `contact_store.py` | AIS/视觉关联、历史、身份与占用状态 |
| `trajectory_features.py` | 分源轨迹和接近特征计算 |
| `contact_assessor.py` | 证据充分性检查与 LLM 研判 |
| `intent_store.py` | 意图 CRUD/到期/版本/指标 |
| `task_catalog.py` | 跨类型候选、可行边、稳定任务 ID |
| `mission_scheduler.py` | 调度快照、选择校验、配对和分配事务 |
| `intent_commands.py` | 线程安全意图命令队列及幂等状态 |
| `episode_logger.py` | 分角色日志与 manifest |
| `outcome_evaluator.py` | 真值隔离效果计算 |
| `strategy_memory.py` | 候选经验、验证报告、版本载入/回滚 |

新增 `src/env/ship_navigation.py`、`src/control/heuristic/probe.py`；修改原有 ship、ais_signal、simulation、schedule、control、vis，不复制出另一套仿真引擎。新增测试主目录 `tests/mission/`；每个文件具体见各任务。

## T01：冻结配置与公共契约

**文件**：新增 `src/mission/__init__.py`、`src/mission/contracts.py`、`src/mission/config.py`、`configs/mission.yaml`、`tests/mission/__init__.py`、`tests/mission/test_contracts.py`、`tests/mission/test_config.py`；修改 `src/schedule/config_loader.py`、`configs/ship.yaml`、`configs/llm_params.yaml`、`tests/test_runtime_configuration.py`。

**输入/输出**：按 D §5 原样定义所有 dataclass；`MissionConfig` 包含 `contact:ContactConfig,intent:IntentConfig,scheduling:SchedulingConfig,evolution:EvolutionConfig`，字段逐一对应 D §4 YAML；AppConfig 增加 mission；ShipConfig 替换为 D §4 的全部 ship 字段。加载器 `load_strict_yaml(path: str) -> dict` 拒绝重复键，`validate_mission_config(config: AppConfig) -> None` 做跨组件范围检查。

- [ ] 写测试：N 为0有效；目标>N、bool N、负概率、NaN、near>=detect、baseline超EO范围、重复 cycles、未知字段均报 ValueError；dataclass 快照不暴露真值。

```python
import pytest
from src.mission.config import load_strict_yaml

def test_duplicate_cycles_rejected(tmp_path):
    path = tmp_path / 'llm.yaml'
    path.write_text('cycles: {max_retries: 2}\ncycles: {max_retries: 3}\n')
    with pytest.raises(ValueError, match='duplicate'):
        load_strict_yaml(str(path))
```

- [ ] 实现 D 字段、枚举及以下公共验证原则；Python bool 不能通过数字判断。

```python
import math

def finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name}: expected number')
    if not math.isfinite(value):
        raise ValueError(f'{name}: expected finite number')
    return float(value)
```

- [ ] loader 用 SafeLoader 的 mapping constructor 检查每层键是否重复；增加 strict dataclass 转换，新域不静默过滤字段。旧 ship 配置拒绝并给明确迁移错误，不同时保留两套数量来源。
- [ ] 增加 `ShipTruth`（仅 env/evaluation）：`ship_id,vessel_class:'type_ii'|'type_i',ais_enabled:bool,normal_route:tuple[Pose,...]`，不要放到我方快照。将身份与 AIS 选择随机子流定义为 manifest 可记录的独立 RNG。
- [ ] 执行 `python -m pytest tests/mission/test_contracts.py tests/mission/test_config.py tests/test_runtime_configuration.py -q`；必须全部通过。老测试涉及旧配置的断言按新契约替换，记录理由。
- [ ] 提交 `feat: define mixed maritime contracts and validated configuration`。

## T02：精确 N 船与无身份捷径 AIS

**文件**：修改 `src/env/ship.py`、`src/env/ais_signal.py`、`src/env/simulation.py::_create_ships`、`tests/env/test_goal2_foundation.py`；新增 `tests/mission/test_ship_population.py`、`tests/mission/test_ais_generation.py`。

**接口**：`create_ship_population(config: AppConfig, seed: int, land_mask: np.ndarray, navigator: AStarNavigator) -> list[Ship]`（放 ship.py，地图由引擎实际环境传入）；Ship 提供 `ship_id/id, vessel_class, ais_enabled, normal_route, pose, float_position, departed`，速度/航向属性与现有帧适配；`generate_ais_signal(ship, timestamp)` 保留原函数名返回 AISSignal|None。

- [ ] 参数化测试 N=0/1/8、type_ii=0/N/混合；数量精确，相同 seed 一致，不同身份随机不改变正常速度/船型标签；所有位置在合法水域，初始出口可达。

```python
def test_type_i_always_broadcasts(population):
    from src.env.ais_signal import generate_ais_signal
    for ship in population:
        if ship.vessel_class == 'type_i':
            assert all(generate_ais_signal(ship, t) is not None for t in (0, 1, 10))
```

- [ ] 替换编队生成：随机打乱 N 个槽位后取 type_ii_count，目标用独立 Bernoulli 决定 AIS；I 类船舶 AIS 强制 on。ID 只取生成序号，不含身份。每船独立路线和位置，失败抛初始化异常。
- [ ] AIS 为两类 on 船使用相同名称/MMSI生成算法、同噪声分布、相同位置/速度报告机制；去掉军用大位移和 UNKNOWN 名字。不从真实身份设置可见 ship_type。
- [ ] 移除 group movement，迁移目标引用到独立 contact；临时旧字段仅供旧帧读兼容，不能形成新仿真身份逻辑。正常行驶同分布、I 类船舶不读取 UAV 参数。
- [ ] 测试：将同一位置/航向/噪声子流的 AIS-on 船真值从 type_i 改 type_ii，报文字段相同（排除不应变化的实体 ID）；signal 不包含 truth。
- [ ] 执行 `python -m pytest tests/mission/test_ship_population.py tests/mission/test_ais_generation.py -q`；完成 N 与 AIS 生成验证。导航可达检查由 T05 最终接通，但本 task 使用已存在 navigator 验证正常路线。
- [ ] 提交 `feat: generate configurable mixed vessels with indistinguishable civil AIS`。

## T03：统一模型网关与失败契约

**文件**：新增 `src/mission/llm_gateway.py`、`tests/mission/test_llm_gateway.py`；修改 `src/schedule/llm_client.py`、`src/schedule/llm_reviewer.py`、`tests/schedule/test_llm_client.py`。

**接口**：

```python
from dataclasses import dataclass
from typing import Callable

@dataclass(frozen=True)
class ModelResult:
    call_id: str
    success: bool
    payload: dict | None
    errors: tuple[str, ...]
    failure_category: str | None

# LLMGateway.request_json(self, *, role: str, snapshot_id: str,
#   system_prompt: str, user_payload: dict,
#   validate: Callable[[dict], tuple[str, ...]]) -> ModelResult
# LLMGateway.request_text(self, *, role: str, snapshot_id: str,
#   system_prompt: str, user_payload: dict) -> ModelResult
```

- [ ] 增加 fixture transport 注入测试，真实默认 transport 仍使用当前 LongCat API。验证四角色绑定都被检查、SDK max_retries=0，应用最多2次修正重试；401/402/403/400直接失败。
- [ ] JSON parser 拒绝重复键、NaN、Infinity、非 object 顶层；code fence 只允许包住单个完整 JSON，不抽取任意中间片段掩盖错误。raw 原样记录。

```python
import json

def reject_constant(value: str):
    raise ValueError(f'non-finite JSON constant: {value}')

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result

def parse_object(raw: str) -> dict:
    result = json.loads(raw, parse_constant=reject_constant,
                        object_pairs_hook=unique_object)
    if not isinstance(result, dict):
        raise ValueError('expected JSON object')
    return result
```

- [ ] 每次失败修正包含原 assistant 输出和结构化错误 user 消息；每次实际 messages 进入 attempt 日志；角色隔离，不共享我方/对方 conversation memory。网关不负责退化业务决策。
- [ ] fixture 测试 `{bad}`→合法 JSON、valid JSON wrong schema→修正、连续失败、timeout、重复键、额外字段；断言 retry 次数、没有 SDK 隐式重试、输出日志无密钥。
- [ ] 执行 `python -m pytest tests/mission/test_llm_gateway.py tests/schedule/test_llm_client.py -q`；迁移 legacy decide wrapper 使用网关且保持其测试可识别。
- [ ] 提交 `feat: add role isolated LLM gateway with strict validation`。

## T04：距离门控与对方集中参数决策

**文件**：新增 `src/mission/red_commander.py`、`src/mission/prompts/red_commander.txt`、`tests/mission/test_red_commander.py`；修改 `src/env/ship.py::set_tracked`（移除逃逸副作用）。

**接口与辅助类型**：

- `RedShipSnapshot(ship_id:str,vessel_class:str,position_cells:Vec2,heading_deg:float,speed_kn:float,normal_tangent_deg:float,gate_state:str,ais_enabled:bool)`。
- `RedSnapshot(snapshot_id:str,sim_time_min:float,ships:tuple[RedShipSnapshot,...],uavs:tuple[tuple[str,Vec2,Vec2],...],active_ship_ids:tuple[str,...],land_mask_version:int)`；uavs 三项为 ID/位置/速度。
- `ThreatGate.update(ship_id, vessel_class, min_distance_cells, now_min)->str`、`observe_swept_distance(ship_id,vessel_class,min_distance_cells,now_min)->None`。
- `RedCommander.decide(snapshot:RedSnapshot)->RedPlan|None`；活动集合非空但无可用计划失败时抛 `RedDecisionBlocked`，由 T12 捕获暂停。

- [ ] 写边界测试 `<`、`==`、`>`，5 min clear hold 被再次接近打断；同帧两舰一次 API；I 类船舶永不接受 command；set_tracked(False/True) 不改变门控。

```python
def test_no_evasion_at_equal_threshold(gate):
    assert gate.update('V1', 'type_ii', 1.5, 0.0) == 'normal'
    assert gate.update('V1', 'type_ii', 1.49, 1.0) == 'evasive'
    assert gate.update('V2', 'type_i', 0.1, 1.0) == 'normal'
```

- [ ] 实现状态机和对相对运动线段的最近距离：r0=ship_start-uav_start，dr=(ship_end-ship_start)-(uav_end-uav_start)，s=clip(-dot(r0,dr)/dot(dr,dr),0,1)，最近距离=norm(r0+s*dr)，零 dr 用 norm(r0)。主步触发不漏跨越。
- [ ] Prompt 明确只能控制 active_ship_ids、运动参数含义和界限；输出严格采用 D RedPlan。校验集合相等、无重复、有限数字、有效期<=3、heading/速度/幅度/周期范围、snapshot匹配；所有 commands 整体接受或整体拒绝。
- [ ] 计划安装时记录起始相位与安装时间；同 snapshot 重复交付幂等。clear 后废弃过期逃逸计划，不让重新接近复用之前旧 snapshot。
- [ ] 失败测试：旧计划未过期继续；已过期或首次失败 RedDecisionBlocked；不能用默认蛇形产生“成功”结果。
- [ ] 活动目标输出与正常参数完全一致应被拒绝；按D最小机动约束检查至少一个方向/幅度/速度差达到条件，不由程序替模型挑选机动。
- [ ] 执行 `python -m pytest tests/mission/test_red_commander.py -q`；提交 `feat: gate fleet LLM evasion by UAV proximity`。

## T05：船舶轨迹生成、Hybrid A* 与实际运动验证

**文件**：新增 `src/env/ship_navigation.py`、`tests/mission/test_ship_navigation.py`；修改 `src/env/ship.py::step`，必要时小范围修改 `src/control/heuristic/navigation.py` 的可复用接口，不改变 UAV 现有默认行为。

**接口**：D §6 的 `ShipNavigator.plan` 与 `ShipRoute`；`Ship.advance(route:ShipRoute,dt_min:float)->tuple[Pose,...]` 返回真实执行子步路径；`Ship.normal_tangent_rad()->float` 由正常路线进度得到。

- [ ] 测试无遮挡时振荡保留；两侧绕岛、凹岸、无路；转弯限制、加速度限制、轨迹端点及采样段不入陆；正常船不因是否被跟踪改变边界策略。

```python
import math

def reference_heading(normal, offset_deg, amplitude_deg, phase_deg, elapsed, period):
    return normal + math.radians(offset_deg) + math.radians(amplitude_deg) * math.sin(
        math.radians(phase_deg) + 2 * math.pi * elapsed / period)
```

- [ ] 参考积分只在当前位置向前 horizon，不每帧累加 offset；相位以实际计划起始时间计算。正常模式从预生成路线取切向，无 UAV 输入。
- [ ] 构建仅水面陆地约束的 mask，按 clearance 膨胀；按 D §6 保留合法参考段，只修复碰撞段。A* 目标使用后续参考点；返回真实动力学滚动验证通过的路径。导航失败降速到停止，输出 blocked；不得穿岛或瞬移到可达点。
- [ ] 将 Ship.turn/yaw 计算拆成纯滚动模拟复用于预测与执行，避免预测和实际采用不同方程；子步位置线段都检查碰撞。驶离需要穿过指定合法水面出口，单船归档，不触发组离场。
- [ ] 回归：`python -m pytest tests/mission/test_ship_navigation.py tests/control/heuristic/test_navigation.py tests/env/test_dubins.py -q`，确认复用未破坏 UAV。
- [ ] 提交 `feat: execute parameterized vessel motion through safe Hybrid A star routes`。

## T06：全局 AIS 与仅观测 ContactStore

**文件**：新增 `src/mission/contact_store.py`、`tests/mission/test_contact_store.py`、`tests/mission/test_visibility.py`；修改 `src/env/simulation.py::_refresh_ais_signals/_update_sensors_and_detections`、`src/schedule/state_manager.py`、`src/control/common/observation.py`。

**接口**：

```python
# ContactStore.ingest_ais(signal: AISSignal, received_at_min: float) -> str
# ContactStore.ingest_visual(detection: VisualDetection) -> str
# ContactStore.snapshot(contact_id: str) -> ContactSnapshot
# ContactStore.list_snapshots() -> tuple[ContactSnapshot, ...]
# ContactStore.expire(now_min: float) -> tuple[str, ...]
# ContactStore.apply_assessment(assessment: Assessment) -> None
# ContactStore.reserve(contact_id: str, uav_id: str, probe_id: str|None) -> None
# ContactStore.release(contact_id: str, now_min: float, reason: str) -> None
```

传感器适配器输出 D §14.1 的 VisualDetection；ingest_visual 成功关联后才构造带contact_id的ObservationSample，不使用空contact_id占位。VisualDetection字段同sample去掉contact_id，observer_position和measured_range必须存在，补入contracts。

- [ ] 写测试：远离所有 UAV 的 AIS 立即建 contact；静默船没有 SAR/EO 不出现；两个相同观测环境替换隐藏身份，我方 snapshot 和 Prompt 完全一致。
- [ ] 实现 D §5.1 最近邻+歧义门控，MMSI 幂等，两次确认合并，alias 与重复任务合并事件。严格按来源记录样本和 revision；不从 Ship.trail 复制历史。
- [ ] StateManager 新增 `contacts:ContactStore`，TargetReport 改为观测适配视图：contact_id 替代 group_id；所有 track 中心来自 contact，过期最多预测 stale_after 分钟，之后失跟。预测不增加 last_seen/有效样本数。
- [ ] 删除我方 `_group_center`、目标真值速度、is_evading 的使用路径；传感器内部允许读取真值用于生成测量，但输出类型只能是 VisualDetection/AISSignal。
- [ ] 用字段白名单构建我方 Prompt 数据，不对 Ship 做 `asdict` 再删字段。增加变化不泄露测试：改变隐藏船数、身份或门控且固定观测，blue payload hash 不变。
- [ ] 执行 `python -m pytest tests/mission/test_contact_store.py tests/mission/test_visibility.py tests/control/test_observation.py -q`；提交 `feat: ingest global AIS and sensor histories into observation only contacts`。

## T07：历史特征与递进证据会话

**文件**：新增 `src/mission/trajectory_features.py`、`tests/mission/test_trajectory_features.py`、`tests/mission/test_probe_session.py`；修改 contracts 增加 D §7.3 TrajectoryFeatures。

**接口**：`build_features(contact:ContactSnapshot,probe:ProbeSession,now_min:float,config:ContactConfig)->TrajectoryFeatures`；`advance_probe(probe:ProbeSession,new_samples:tuple[ObservationSample,...],now_min:float,config:ContactConfig)->ProbeSession`；`select_keypoints(samples,limit:int)->tuple[ObservationSample,...]`。

- [ ] 测试359°→1°为2°变化；重复时间戳不除零；AIS/EO偏移不算加速；大缺测中断 exposure；近距离AIS独自不足；绕岛标记 confounder。

```python
def wrap_delta_deg(current: float, previous: float) -> float:
    return (current - previous + 180.0) % 360.0 - 180.0

def test_heading_wrap():
    assert wrap_delta_deg(1.0, 359.0) == 2.0
```

- [ ] baseline/near 累计有效测量间隔：相邻有效同源样本时差<=2且均处于当前 phase，才累计；phase切换后独立样本计数；近距离<=near+0.15。先暴露后回到外圈不能凭墙钟记够近距离时长。
- [ ] 测试长距离转场不消耗20min证据窗口；approach独立120min上限，首次baseline_started_at固定，后续phase切换/样本重置不无限延长核查总窗口。
- [ ] sufficient_evidence 要求基线与近距离时长/样本均满足，且存在本 UAV 接近上下文；confounded 不直接判 type_i。关键点保留首尾、开始接近、最小距离、最大转向及其相邻点，其余均匀补齐，最多12且稳定排序。
- [ ] 速度/转率使用同源连续样本和 D 中的三点平滑；feature 每项 null 处理、单位转换、证据 ID 能回查。features 的 revision 等于构建时 snapshot revision。
- [ ] 执行 `python -m pytest tests/mission/test_trajectory_features.py tests/mission/test_probe_session.py -q`；提交 `feat: derive approach correlated trajectory evidence`。

## T08：真实 LLM 身份研判替代 AIS 规则分类

**文件**：新增 `src/mission/contact_assessor.py`、`src/mission/prompts/contact_assessor.txt`、`tests/mission/test_contact_assessor.py`；修改 `src/utils/ais_discriminator.py`、`tests/env/test_ais_discrimination.py`。

**接口**：`ContactAssessor.assess(contact:ContactSnapshot,probe:ProbeSession,features:TrajectoryFeatures,now_min:float)->Assessment|None`。None 表示无足够新证据或调用失败，不等价 type_i。`validate_assessment_payload(payload:dict,contact,probe,features)->tuple[str,...]`。

- [ ] fixture 测试同 AIS 状态可得到 type_ii/type_i/unknown；未完成 near 即返回 type_i 被拒绝；证据 ID 外来或 revision 错被拒绝；无新 revision 不重复调用。

```python
def test_ais_enabledly_cannot_clear(assessor, ais_enabledly_contact, empty_probe):
    from src.mission.trajectory_features import build_features
    f = build_features(ais_enabledly_contact, empty_probe, 5.0, assessor.config)
    assert not f.sufficient_evidence
    assert assessor.assess(ais_enabledly_contact, empty_probe, f, 5.0) is None
```

- [ ] Prompt 规定民用 AIS 是声称身份；不以静默或位置一致终判；比较接近前后行为并考虑陆地/噪声/已有接近。模型只能引用真实样本，理由和替代解释限各3项、每项200字，控制日志体积。
- [ ] 程序生成 assessment_id/call_id/time，payload 只提供 D JSON 允许键。confidence 不当校准概率，UI注明为模型置信度；所有终判均执行证据门槛。
- [ ] 从运行时移除 discriminate/discriminate_formation 调用；保留 `estimate_target_position` 或迁移到测量工具。旧“silent means type_ii”测试替换为“silent means unknown until observed”，记录需求变更，不保留反向兼容开关使旧错误逻辑复活。
- [ ] 执行 `python -m pytest tests/mission/test_contact_assessor.py tests/env/test_ais_discrimination.py -q`；提交 `feat: assess contact vessel_class from validated trajectory evidence`。

## T09：递进控制器与资源释放语义

**文件**：新增 `src/control/heuristic/probe.py`、`tests/control/heuristic/test_probe.py`、`tests/mission/test_contact_release.py`；修改 `src/control/common/contracts.py`、`src/control/common/observation.py`、`src/control/common/factory.py`、`src/control/common/operation_registry.py`、`src/control/common/safety.py`、`src/control/heuristic/task_flow.py`、`src/control/heuristic/tracking.py`、`configs/control.yaml`。

**接口**：`OperationMode.PROBE`；`ControlTask` 增加 `probe_id:str|None=None`，`ProbeController` 实现现有 ControllerBase observation_spec/action_spec/start_task/act/is_complete/stop_task；事件 `probe_phase_changed`,`probe_blocked`,`probe_timed_out`,`contact_assessed`,`mission_task_released`。

- [ ] 测试控制器只使用 ContactObservation；先baseline再near；无可达近轨道发 blocked；缺测不推进阶段；安全包络/燃油返航有效；旧 generation 命令拒绝。
- [ ] 控制器通过现有 navigator 和 tracking orbit helper 形成两种 standoff；不要复制整个 TrackingController。将公共“到观测目标的轨道规划”抽成 tracking.py 的纯 helper，控制器仍保持单一任务。
- [ ] 枚举扩展同步 action mask、factory、registry、frame、operation transition；probe 使用EO，不把核查期间运动算区域 SAR 覆盖。
- [ ] observation升级为v2，增加D §14.2冻结probe/裁剪history字段；factory通过显式ContactConfig构造ProbeController，不能从控制器内读全局配置文件；会话只有仿真线程推进，控制器不复制时长状态机。
- [ ] 身份 type_ii 转 TRACK，保留同 contact 占用但替换 controller generation；type_i 转 SYSTEM holding/基地idle，清除 contact/region/uav 三方指针和 saved coverage，发布 resource_available。安全返航不被释放改成 idle。

```python
def test_type_i_release_does_not_resume_saved_search(release_fixture):
    release_fixture.apply_type_i_assessment()
    assert release_fixture.contact.assigned_uav_id is None
    assert release_fixture.state.get_uav('UAV-1').assigned_region_id is None
    assert release_fixture.coordinator.operation_mode('UAV-1').value == 'holding'
    assert release_fixture.new_search_installations == 0
```

- [ ] 删除 `target_found` 自动抢占任务转换；只保留 `mission_assignment_approved` 触发全局调度授权的新任务。事件序列幂等；重复释放不增加两次资源事件。
- [ ] 执行 `python -m pytest tests/control/heuristic/test_probe.py tests/mission/test_contact_release.py tests/control/test_coordinator.py tests/control/test_ownership.py tests/control/heuristic/test_task_flow.py -q`；提交 `feat: add approach investigation control and atomic contact release`。

## T10：重点意图状态与候选加权

**文件**：新增 `src/mission/intent_store.py`、`tests/mission/test_intent_store.py`、`tests/mission/test_intent_candidates.py`；修改 `src/schedule/info_field.py`、`src/schedule/candidate_extractor.py`、contracts 增加 IntentStatus。

**接口**：`IntentStore.create(data:dict,now_min:float)->Intent`、`update(intent_id,expected_revision,changes,now_min)->Intent`、`cancel(intent_id,expected_revision,now_min)->Intent`、`expire(now_min)->tuple[Intent,...]`、`active()->tuple[Intent,...]`、`evaluate(info,last_scan,searchable_mask,tasks,now_min)->tuple[IntentStatus,...]`；`build_scheduling_value(base_value,intents,last_scan,now_min)->np.ndarray`。

- [ ] 测试创建/修改/取消/到期版本严格递增；全陆地拒绝、部分陆地正确分母；未知字段/非整数bbox拒绝；重叠意图 max 不叠加；原 base matrix 不变。

```python
def test_intent_weight_does_not_change_base_value(intent_case):
    import numpy as np
    from src.mission.intent_store import build_scheduling_value
    original = intent_case.base_value.copy()
    result = build_scheduling_value(original, intent_case.intents,
                                    intent_case.last_scan, 20.0)
    assert np.array_equal(original, intent_case.base_value)
    assert result[intent_case.focus_cell] > original[intent_case.focus_cell]
```

- [ ] 把候选提取扩展为可接收 scheduling_value 和 intents；不要将“重点区 bbox”直接当搜索 bbox。面积/曲率/航迹约束继续检查。去重合并 intent_ids，按 D §8 预留普通名额与意图轮询。
- [ ] 候选上限必须与当前可用资源解耦：没有 idle 但有可抢占资源时仍能提供候选；可保留现役区域，但不能让 lifecycle 模式吞掉 intent/probe 资源。
- [ ] 评价保持固定水域分母，天气只改变可执行性；对从未扫描 max_scan_age 返回null并给未扫描数，不序列化 infinity。intent expiry 删除加权而不清信息场。
- [ ] 执行 `python -m pytest tests/mission/test_intent_store.py tests/mission/test_intent_candidates.py tests/schedule/test_candidate_extractor.py tests/schedule/test_info_field.py -q`；提交 `feat: incorporate operator focus areas into candidates and freshness metrics`。

## T11：统一任务候选、LLM 选择与可行资源配对

**文件**：新增 `src/mission/task_catalog.py`、`src/mission/mission_scheduler.py`、`src/mission/prompts/mission_scheduler.txt`、`tests/mission/test_task_catalog.py`、`tests/mission/test_mission_scheduler.py`；修改 `src/schedule/task_allocator.py`、`src/schedule/trigger_manager.py`、`src/schedule/hungarian.py`、`src/schedule/prompt_builder.py`、`src/schedule/output_validator.py`。

**接口与事务类型**：

- `TaskCatalog.build(state:StateManager,contacts:tuple[ContactSnapshot,...],intents:tuple[Intent,...],now_min:float)->tuple[TaskCandidate,...]`。
- `MissionSnapshot` 完整字段使用 D §14.1，包含 resources/feasible_edges/active_tasks/intent_statuses/planning_map_version/reviewer_summary；不能只传候选ID丢掉成本、油量和现役任务。`TaskRecord`和`FeasibleEdge`同节定义。
- `Assignment(task_id:str,uav_id:str,expected_generation:int,previous_task_id:str|None)`、`AssignmentBatch(snapshot_id:str,assignments:tuple[Assignment,...],selection_call_id:str)`。
- `MissionScheduler.decide(snapshot:MissionSnapshot)->AssignmentBatch|None`；`validate_selection(payload,snapshot)->tuple[str,...]`；`pair_selected_tasks(selection:MissionSelection,snapshot:MissionSnapshot)->tuple[Assignment,...]`。

- [ ] fixture 测试新 AIS 与 SAR 同候选竞争、核查优先选择、容量满抢占普通搜索、不能抢返航/跟踪；N>UAV 排队；不可达边过滤、Hall 条件冲突、未知 task ID、重复 contact、重叠搜索拒绝。

```python
def test_matching_detects_shared_only_uav(make_snapshot, selection):
    from src.mission.mission_scheduler import validate_selection
    snapshot = make_snapshot(task_edges={'Q1': ('U1',), 'Q2': ('U1',)})
    payload = selection(['Q1', 'Q2'], snapshot.snapshot_id)
    assert 'infeasible_assignment' in validate_selection(payload, snapshot)
```

- [ ] Catalog 为 pending contact 创建probe、未分配target创建track、候选区域创建search；同 contact 保持单任务 ID。cleared 普通AIS不创建候选。候选年龄保留，超prompt限制不删除存储状态。
- [ ] 实现D §14.1 TaskRecord状态机和抢占搜索的保留/冲突取消；新candidate不等于approved，任务执行日志不能仅复用TaskCandidate。
- [ ] 计算每 UAV→任务可行路线和航程，调用已存在返航评估，不仅看欧氏距离；route cache 按 map_version/pose/goal/generation失效。成本为真实计划转场时间，保留禁止边掩码。先最大匹配验证，再 Hungarian 优化；无SciPy可用明确失败或使用经过验证的精确小规模匹配，不使用贪心冒充可行最优。
- [ ] 调度 Prompt 使用新 schema，移除“恰好选够全部 SAR”规则；模型选择 task ID 和必要 preempt。preempt集合必须等于实际匹配中从普通搜索转新probe/track的 UAV 集合；有多解时匹配先约束此集合，不能随意配到idle后再错误报unused preempt。
- [ ] 几何与语义校验：矩形范围面积继续复用旧 validator 核心，将写死10替换 config；probe/track不走搜索矩形面积检查。前序有效任务保留，不把未选择新增任务视为删除现役任务。
- [ ] 触发器用 contact_created/assessment_changed/resource_available/intent_changed/intent_expired；同帧合并，AIS普通更新无heavy，事件按ID幂等。light仅重新配已批准任务。
- [ ] 执行 `python -m pytest tests/mission/test_task_catalog.py tests/mission/test_mission_scheduler.py tests/schedule/test_trigger_manager.py tests/schedule/test_task_allocator.py tests/schedule/test_output_validator.py -q`；提交 `feat: select and match search investigation and tracking tasks globally`。

## T12：主循环接线、原子任务安装与旧逻辑退出

**文件**：修改 `src/env/simulation.py`、`src/schedule/state_manager.py`、`src/control/common/coordinator.py`（仅必要事务扩展）、`src/control/heuristic/task_flow.py`；新增 `tests/mission/test_simulation_flow.py`、`tests/mission/test_failure_paths.py`；更新 `tests/env/test_simulation_integration.py`、`tests/control/test_simulation_ownership.py`。

**接口**：`SimulationEngine.apply_assignment_batch(batch:AssignmentBatch)->bool`；`SimulationEngine.retry_blocked_decision()->None`；只读 `runtime_status:'running'|'paused_model'|'finished'|'failed'` 与 `blocked_role:str|None`。引擎初始化接受 `llm_gateway=None` 测试依赖注入，None 使用真实网关；fixture manifest 标志自动设定。

- [ ] 写测试模拟完整AIS→probe→assessment→release链；控制器generation跟随安装变化，失败准备不停止旧任务；red首次失败时间不前进；blue失败旧任务继续；assessor失败不判I 类船舶。

```python
def test_red_failure_does_not_advance_clock(blocked_red_engine):
    before = blocked_red_engine.clock.time
    blocked_red_engine.step()
    assert blocked_red_engine.clock.time == before
    assert blocked_red_engine.runtime_status == 'paused_model'
    assert blocked_red_engine.synthetic_decision_count == 0
```

- [ ] 依 D §9 顺序重排 step：不要开头先 tick 再发生 red blocked。双方运动使用同一开始快照，先计算各控制意图再运动，不能一艘更新后另一艘读已移动位置产生顺序偏差。
- [ ] 引擎初始化摄入t=0卫星AIS，首轮调度包含新接触；N初始化后不因离场补船。实现D §14.4 queued与候选未获批语义，避免创建无配对的假任务。
- [ ] 批量任务准备所有路由和controller、检查快照/意图revision/generation；通过后串行事务提交。对每次提交记录可恢复状态；若现有 coordinator 只支持单 UAV，新增 batch prepare/commit 在同一锁内先做全量验证、再无失败构造提交，不在提交中调用API或规划。
- [ ] 去掉 `_handle_detection` 的任务抢占、`_process_ais_tracking` 身份判别、`_sync_state_from_entities` 真值跟踪中心、set_tracked逃逸、组离场。更新 lifecycle/search completion/freshness patrol 使其只产生任务事件，不越过 scheduler 自动夺取刚释放资源。
- [ ] 核查目标转跟踪的有效assessment走已批准任务延续；I 类船舶/失跟/超时释放事件幂等；sensor释放不能终止正在安全返航的controller。
- [ ] reset 创建全新 contacts/gates/probes/tasks/intents/queue，清相位和去重表，保留显式选择的memory manifest；退出只读归档，无法通过reset复用已结束episode命令。
- [ ] 执行 `python -m pytest tests/mission/test_simulation_flow.py tests/mission/test_failure_paths.py tests/control/test_simulation_ownership.py tests/env/test_simulation_integration.py -q`；提交 `feat: integrate observable mixed maritime mission lifecycle`。

## T13：重点区命令队列与 HTTP API

**文件**：新增 `src/mission/intent_commands.py`、`tests/mission/test_intent_commands.py`、`tests/env/test_intent_api.py`；修改 `src/vis/backend/server.py`、`src/env/simulation.py`、`main.py` 的 app/engine 绑定。

**数据/接口**：

- `IntentCommand(command_id:str,episode_id:str,operation:'create'|'update'|'cancel',intent_id:str|None,expected_revision:int|None,payload:dict)`。
- `CommandResult(command_id:str,status:'queued'|'applied'|'rejected',intent:Intent|None,error_code:str|None)`。
- `IntentCommandQueue.enqueue(command)->CommandResult`、`drain()->tuple[IntentCommand,...]`、`complete(result)->None`、`get(command_id)->CommandResult`。
- API、请求字段及状态码严格按 D §10；server 从 app.state.intent_service 访问队列+published snapshot，不直接写引擎状态。

- [ ] HTTP测试202入队但未应用、tick后applied、同命令幂等、不同payload重复409、revision并发冲突、旧episode409、格式422、满队列429、回放禁止409。

```python
def test_http_request_does_not_mutate_simulation(api_client, engine, intent_payload):
    response = api_client.post('/api/intents', json=intent_payload)
    assert response.status_code == 202
    assert engine.intents.active() == ()
    engine.apply_pending_intent_commands()
    assert len(engine.intents.active()) == 1
```

- [ ] 使用 bounded Queue + lock 维护幂等索引，read返回不可变副本；payload hash规范化sorted JSON。入队前验证格式，应用时再次验证expected_revision/episode/水域/意图上限。
- [ ] CORS 方法增加POST/PATCH/DELETE，保持现有允许origin，不引入无关认证体系。命令错误返回固定code+人可读message，不返回堆栈。paused_model可接收待应用命令，不能偷偷推进时钟。
- [ ] 加入D §10 runtime retry/abort两条接口及独立运行命令队列，仿真线程在paused状态仍处理retry/abort；重试不自动应用新的意图到旧snapshot，abort归档且不删数据。运行命令同episode/command_id幂等。
- [ ] 将意图事件加入 frame，前端通过command查询或WS确认应用。main app与engine必须同一队列，避免server新建孤立IntentStore。
- [ ] 执行 `python -m pytest tests/mission/test_intent_commands.py tests/env/test_intent_api.py tests/env/test_server_runtime.py -q`；提交 `feat: queue versioned operator intent mutations through the simulation thread`。

## T14：人工框选、接触详情与兼容回放

**文件**：修改 `src/vis/frontend/src/components/CanvasMap.jsx`、`src/vis/frontend/src/renderer/geometry.js`、`src/vis/frontend/src/renderer/layers.js`、`src/vis/frontend/src/App.jsx`、`src/vis/frontend/src/App.css`、`src/vis/backend/frame_builder.py`；新增 `src/vis/frontend/src/components/IntentPanel.jsx`、`src/vis/frontend/src/components/ContactPanel.jsx`、`src/vis/frontend/tests/mixed-maritime.spec.js`、`tests/env/test_mixed_frame.py`。

**帧契约**：新增 `schema_version:'mission-frame/v2',episode_id,contacts:ContactSnapshot[],intents:Intent[],intent_statuses:IntentStatus[],intent_events:CommandResult[],runtime_status,blocked_role,memory_version`。默认 contacts samples只含关键点与证据引用，完整历史查日志；物理 ships 数组仅truth层时返回，旧回放字段兼容。

- [ ] 先写Playwright案例：四方向拖选同bbox，缩放/高DPI正确，Esc取消，拖出边界截断，点击无矩形，表单提交看到queued→active，到期变灰，回放无写按钮。固定API/WS fixture只用于UI测试。
- [ ] 归一化计算以canvas的CSS坐标为准，参考实现：

```javascript
export function dragToBBox(a, b, layout, cols, rows) {
  const clamp = (v, max) => Math.max(0, Math.min(max, v));
  const x0 = (Math.min(a.x, b.x) - layout.offsetX) / layout.cellSize;
  const y0 = (Math.min(a.y, b.y) - layout.offsetY) / layout.cellSize;
  const x1 = (Math.max(a.x, b.x) - layout.offsetX) / layout.cellSize;
  const y1 = (Math.max(a.y, b.y) - layout.offsetY) / layout.cellSize;
  if (Math.abs(a.x-b.x) < 3 || Math.abs(a.y-b.y) < 3) return null;
  const bbox = [clamp(Math.floor(x0), cols), clamp(Math.floor(y0), rows),
                clamp(Math.ceil(x1), cols), clamp(Math.ceil(y1), rows)];
  return bbox[0] < bbox[2] && bbox[1] < bbox[3] ? bbox : null;
}
```

- [ ] pointerdown仅任务水域方框内开始；pointer capture + pointercancel/blur清理；开启框选时禁用UAV选择事件。按D表单字段构造命令，episode改变清除未提交草稿并提示。
- [ ] ContactPanel显示“声称民用AIS”而非“I 类船舶”，研判证据和phase；未观察船不能在默认层提前出现；truth层明确标记“评估真值”。不把model_call_id/schema暴露成主流程表单。
- [ ] paused_model面板显示阻塞角色和失败原因，提供重试/结束本回合按钮调用runtime接口；不能通过浏览器计时器继续推进仿真画面。增加成功重试与归档终止的UI测试。
- [ ] 旧帧 `contacts/intents ?? []`；播放使用该帧意图状态，不能套用当前live意图。只读模式服务端同样拒绝写。
- [ ] 执行 `python -m pytest tests/env/test_mixed_frame.py tests/env/test_frame_publisher.py -q`；在前端执行 `npm run build`、`npx playwright test tests/mixed-maritime.spec.js`、`npm run test:acceptance`。缺浏览器先安装项目指定Playwright browser并记录，不跳过视觉验收。
- [ ] 提交 `feat: add operator focus selection and evidence based contact visualization`。

## T15：分域日志、回合指标与数据泄露检查

**文件**：新增 `src/mission/episode_logger.py`、`src/mission/outcome_evaluator.py`、`tests/mission/test_episode_logger.py`、`tests/mission/test_outcome_evaluator.py`；修改 `src/vis/backend/frame_logger.py`、`src/env/simulation.py::summary`、`scripts/evaluate_goal.py` 的旧指标兼容输出。

**接口**：`EpisodeLogger.start(manifest:dict)->str`、`append(domain:str,stream:str,record:dict)->None`、`finish(status:str)->None`；`OutcomeEvaluator.observe(tick:EvaluationTick)->None`；`finalize()->EpisodeOutcome`。EvaluationTick/VesselTruthSample及新增终判覆盖率字段按D §14.3，必须使用实际TaskRecord与有效EO链接，不用静态ShipTruth/候选任务代替执行数据。

- [ ] 写已知时长例子：两艘目标在场各10min，正确EO跟踪共5min，target_tracking=0.25；10minI 类船舶核查/100min UAV active=0.1；无目标tracking=null；无终判accuracy=null且unknown数量明确。

```python
def test_unknown_does_not_count_as_correct(evaluation_fixture):
    outcome = evaluation_fixture.finish_with_no_terminal_assessments()
    assert outcome.classification_accuracy is None
    assert evaluation_fixture.metrics['unknown_contacts'] > 0
```

- [ ] 指标只计算已实际执行时间，不把selected任务当已完成；任务跨决策共享效果用回合聚合，不声称单次LLM因果贡献。contact alias映射在评估器结合真值解算，错关联造成的错跟计入错误，不通过真实ID修正我方状态。
- [ ] D §9 路径和schema写jsonl，限制为声明domain；使用episode唯一目录防覆盖，final manifest原子替换。密钥/Authorization永不进入日志；model roles request记录都包含memory版本与实际attempt。
- [ ] 保存decision episode、intent revision、controlled experiment种子分支、有效/无效原因。API故障导致不完整核查的回合不能作为分类成功和自演进支持。
- [ ] 执行 `python -m pytest tests/mission/test_episode_logger.py tests/mission/test_outcome_evaluator.py tests/mission/test_visibility.py -q`；提交 `feat: record reproducible role separated mission outcomes`。

## T16：候选策略经验与受限记忆注入

**文件**：新增 `src/mission/strategy_memory.py`、`src/mission/prompts/strategy_reviewer.txt`、`tests/mission/test_strategy_memory.py`；修改 `src/schedule/llm_reviewer.py`、`src/mission/mission_scheduler.py`。

**接口**：`StrategyMemoryStore.propose(outcomes:tuple[EpisodeOutcome,...],decision_summaries:tuple[dict,...])->StrategyMemory|None`；`load_manifest(version:str)->tuple[StrategyMemory,...]`；`select_for_context(context:dict,version:str)->tuple[StrategyMemory,...]`。持久化路径 `outputs/strategy_memory/`，manifest是JSON，记录memory IDs+内容hash+parent_version，不存可执行脚本。

- [ ] 测试支持回合少于3、fixture回合、同回合真值片段、包含ship/contact ID、非法condition键、超过400字、code payload均不能进入候选；候选不进入活跃Prompt。

```python
def test_candidate_is_not_active(memory_store, candidate_memory):
    memory_store.save_candidate(candidate_memory)
    assert memory_store.select_for_context(
        {'has_active_intent': 'yes'}, 'baseline') == ()
```

- [ ] Reviewer输入聚合指标和去身份决策摘要，Prompt明确提出可验证调度建议，不能改探测半径、身份阈值和导航安全；applies_when使用D白名单，不eval字符串。检查理由引用已完成episode及真实指标。
- [ ] 现有15min态势Reviewer继续仅当前观测摘要，与离线策略Reviewer使用不同system prompt和存储。策略记忆只注入mission_scheduler；不注入red或assessor。
- [ ] manifest版本在episode初始化固定；中途激活不改变正在运行快照；渲染显示memory version。context未知键不匹配，默认baseline空记忆。
- [ ] 执行 `python -m pytest tests/mission/test_strategy_memory.py tests/mission/test_mission_scheduler.py -q`；提交 `feat: propose bounded evidence backed scheduling memories`。

## T17：成对验证、holdout、激活与回滚

**文件**：新增 `scripts/evaluate_mixed_maritime.py`、`scripts/validate_strategy_memory.py`、`tests/mission/test_strategy_validation.py`、`tests/mission/test_evaluation_cli.py`；修改 strategy_memory.py 增加验证状态机。

**接口**：`ValidationReport(report_id:str,memory_id:str,baseline_version:str,validation_episode_pairs:tuple[tuple[str,str],...],holdout_episode_pairs:tuple[tuple[str,str],...],mean_score_gain:float|None,component_deltas:dict[str,float],type_ii_misclassified_as_type_i_delta:float|None,passed:bool,reasons:tuple[str,...])`；`evaluate_paired_outcomes(baseline,candidate,config)->ValidationReport`；`activate(memory_id,report_id)->str`；`rollback(version)->str`。

- [ ] 纯测试固定 outcomes 验证score改善但type_ii_misclassified_as_type_i上升拒绝、holdout退化拒绝、全部fixture拒绝、缺有效pair拒绝、baseline/candidate配置不同拒绝、成功只在回合边界激活。

```python
def test_type_ii_misclassification_regression_rejects(validation_case):
    report = validation_case.compare(score_gain=0.10,
                                     type_ii_misclassified_as_type_i_delta=0.01)
    assert not report.passed
    assert 'type_ii_misclassification_regression' in report.reasons
```

- [ ] 实现D §11指标和门槛；validation seeds与holdout完全分离；每个seed的双方对抗独立运行但固定外生场景/传感器随机子流，禁用原始全局random使模型调用次数影响下一次噪声抽样。随机键按episode seed/entity/time/channel取样。
- [ ] 同步D §14.3终判覆盖率门槛、pair一致分母及缺失指标规则，增加“candidate大量unknown使accuracy表面提高”的拒绝测试。默认完整验证90个live回合，dry-run明确报告此规模和当前每角色调用上限估算。
- [ ] 不同我方策略会改变对方响应，所以两个分支都调用red API；不能用baseline red轨迹当candidate的“真实响应”。固定red模型/Prompt版本，记录非确定性限制；至少3重复，输出配对增益分布和置信区间，不保证模型完全可复现。
- [ ] 新CLI命令规范：

```bash
python scripts/evaluate_mixed_maritime.py --scenario mixed-ais --seed 42 --steps 120 --live --output-dir outputs/evaluations/review-run
python scripts/validate_strategy_memory.py --memory-id M0001 --baseline-version baseline --phase validation --live --output-dir outputs/evaluations/memory-M0001
python scripts/validate_strategy_memory.py --memory-id M0001 --baseline-version baseline --phase holdout --live --output-dir outputs/evaluations/memory-M0001-holdout
python scripts/validate_strategy_memory.py --activate M0001 --report-id REPORT_ID
python scripts/validate_strategy_memory.py --rollback baseline
```

命令一次只允许 run/activate/rollback一种模式；输出目录已存在且包含manifest则拒绝覆盖；activate读取报告hash和两阶段结果，不接受手填passed；无--live仅输出预计回合数/调用预算，不自动请求API。REPORT_ID由验证命令输出，为运行时真实ID而非固定字面值。
- [ ] 执行 `python -m pytest tests/mission/test_strategy_validation.py tests/mission/test_evaluation_cli.py -q`；live验证须在实施阶段显式运行并记录预算，未实际完成不激活记忆。
- [ ] 提交 `feat: validate promote and roll back scheduling memories with held out episodes`。

## T18：全场景验收、文档迁移与 Luna Max 交付

**文件**：新增 `tests/mission/test_end_to_end.py`、`tests/mission/conftest.py`、`docs/MIXED_MARITIME_VALIDATION.md`；更新 `README.md`、`docs/SYSTEM_PARAMS.md`、`docs/VALIDATION.md`、`src/control/interface.md`。旧 GOAL/GOAL2 标记为历史设计并链接新设计，不删除历史结果。

**fixtures**：测试目录中创建明确 `fixture` 标记的role transport；scenario factory生成 `mixed-ais,all-type_i,silent-type_ii,disguised-type_ii,island-confounder,no-resources,intent-overlap,model-failure` 八个场景，支持seed与身份覆盖。只能测试模块注入fixture，生产CLI不增加“mock model成功”开关。

- [ ] 为D V01–V18逐项填测试路径和结果；每个场景至少一个端到端断言，不只检查返回200或json有字段。

```python
def test_disguised_target_requires_investigation(mixed_engine):
    mixed_engine.advance_until_first_ais()
    contact = mixed_engine.first_contact()
    assert contact.vessel_class == 'unknown'
    assert not mixed_engine.has_terminal_assessment(contact.contact_id)
    mixed_engine.advance_until_probe_evidence()
    assert mixed_engine.probe_evidence(contact.contact_id).sufficient_evidence
    mixed_engine.advance_until_assessed()
    assert mixed_engine.first_contact().vessel_class == 'type_ii'
    assert mixed_engine.control_installations_are_authorized()
```

- [ ] 覆盖I 类船舶始终AIS、不会因接近转向，AIS一致伪装船仍需近观察；核查不足/被遮蔽保持unknown；所有资源忙可预占但必须真实有效LLM选择；释放I 类船舶后无自动旧搜索恢复；重点区到期影响后续选择；没有合格经验保持baseline。
- [ ] 运行 `python -m pytest tests/mission tests/control tests/schedule tests/env tests/utils tests/test_runtime_configuration.py -q`，检查失败和skip原因，不全局屏蔽旧测试；所有旧身份/编队断言有迁移说明。
- [ ] 在前端运行 `npm run build`、`npx playwright test`；真实浏览器验证框选/修改/取消、live与replay切换和模型暂停状态。
- [ ] 使用evaluate_mixed_maritime脚本完成固定场景live smoke（至少mixed-ais、all-type_i、disguised-type_ii、island-confounder），逐角色检查真实API日志。没有足够近距离观测的场景不能写成分类通过；记录执行时长和不足原因。
- [ ] 另跑性能评估集，按D末节报告分类准确率、终判覆盖率、误放目标比例、I 类船舶资源占用及重点区满足程度；自演进验证单列，未通过时功能实现可交付但不得宣称已经提升。
- [ ] 更新说明：默认LongCat-2.0四角色；N配置；全局卫星AIS假设；两侧信息边界；参数逃逸；Hybrid A*；核查状态机；人工意图API；旧回放兼容；角色故障处理；如何复核日志与回滚记忆。
- [ ] 提交 `test: validate mixed maritime LLM missions and document operational contracts`。
- [ ] 最终交付仅报告已实现内容、测试证据、live表现及限制、提交范围；不自动部署、不合并、不覆盖本设计的用户审阅记录。

## 19. 贯穿任务的补充校验（每个 reviewer 必查）

1. **身份泄露**：blue上下文中搜索 `vessel_class_truth,vessel_class,is_evading,gate_state,RedPlan`；出现只允许在测试黑名单或评估代码，不能存在序列化字段。不能把physical Ship传给PromptBuilder。
2. **旧逻辑绕过**：`set_tracked` 不能触发机动；`target_found` 不能直接安装TRACK；AIS silence/discrepancy不做终判；`_group_center`不更新我方contact；释放不直接恢复旧区域；lifecycle不能代替LLM新增任务。
3. **时间一致性**：AIS、视觉、UAV接近样本时间可对齐；延迟/缺测不重写历史；API失败重试不推进仿真；phase不按浏览器帧数累计。
4. **几何一致性**：从像素到格到km只有一次转换；bbox半开；NAV radians与API degrees显式转换；A*几何路径真实动力学能执行；全地图尺寸由config控制。
5. **资源守恒**：一个UAV一个owner，一个contact最多一个活动probe/track，事务失败不丢旧任务；没有可行边不会产生虚构配对；已释放I 类船舶不重复排队。
6. **证据完整性**：终判有probe和sample引用，不能把AIS-only或unknown计作分类成功；自演进报告不能从fixture产生。
7. **可重现性**：run配置/模型/Prompt/记忆版本/seed/角色调用可追踪；相同模型设置不声称响应逐字确定；评估分支共享外生随机条件而不共享动态对方轨迹。

## 20. 需求到任务追踪

| 需求 | 实现任务 | 验证场景 |
|---|---|---|
| R1 N通用船 | T01,T02,T05 | V01,V05 |
| R2 I 类船舶干扰、持续AIS、规则航行 | T02,T04,T08 | V02,V03,V09 |
| R3 AIS全局、伪装/静默、身份未知 | T02,T06,T08 | V02,V06,V08,V16 |
| R4 对方LLM参数逃逸与距离门控 | T03,T04,T05,T12 | V03,V04,V15 |
| R5 复用导航 | T05,T09 | V05,V10,V18 |
| R6 历史研判与统一调度 | T06–T09,T11,T12 | V07–V11,V16 |
| R7 I 类船舶释放与去重 | T06,T09,T11,T12 | V12 |
| R8 人工重点区持续调度 | T10,T11,T13,T14 | V13,V14 |
| R9 自演进 | T15,T16,T17 | V17 |
| R10 审阅后实施 | 本文状态与任务执行门 | 本轮只产生文档 |

## 21. 给 Luna Max 的执行提示

用户批准后，可将下面文字连同两份文档交给执行者：

> 请执行 `docs/superpowers/plans/2026-09-14-mixed-maritime-llm-implementation-plan.md`，以配套设计文档为行为和数据契约。先确认用户对默认提案的审阅修改已同步，按T01–T18依赖顺序推进。每个task先实现有意义的契约测试，再实现业务，运行指定检查并记录证据。保留现有ControlCoordinator所有权和安全约束；真实仿真走真实LLM，测试fixture必须隔离标记。不得以静默或AIS一致性判身份，不得把隐藏真值传给我方，不得用规则替代模型新增任务或逃逸决策。完成后交付代码、测试和实际性能报告，不自动merge或部署。

当前请不要执行这段提示；等待用户对设计和方案的审阅。
