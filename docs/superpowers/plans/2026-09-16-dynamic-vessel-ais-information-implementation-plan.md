# I/II Class Dynamic Vessel, AIS, and Information Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> 当前状态：待用户审阅，禁止开始业务代码实施。设计唯一来源为 `docs/superpowers/specs/2026-09-16-dynamic-vessel-ais-information-design.md`（下称 D）。

**Goal:** 将全仓船舶分类统一为 I 类/II 类，使船舶可在实时仿真期间增删、II 类 AIS 可动态切换、红方 LLM 按 UAV 递进侦察阶段管理动态数量的 II 类船舶，并让所有信息变化进入一个版本化触发闭环。

**Architecture:** 保持现有单仿真写线程。HTTP 只入队，`SimulationEngine.step()` 在帧边界原子应用船舶命令；`Ship.ais_enabled` 是 AIS 发射唯一状态，`SurveillanceStageRegistry` 是环境侧侦察阶段唯一状态，`InformationUpdatePolicy` 是 I/S/A/V 与版本唯一状态。蓝方 LLM 仍选择任务，红方 LLM 只返回 II 类船舶机动参数，轨迹继续由确定性导航与动力学执行。

**Tech Stack:** Python 3、dataclasses、numpy、PyYAML、FastAPI、React 18、lucide-react、Canvas、pytest、Playwright。不得新增数据库、消息队列、状态管理框架或新的轨迹控制模型。

## Global Constraints

- 未经用户批准本计划，不得修改业务代码；本计划获批后仍必须按 TDD 的 RED -> GREEN -> REFACTOR 顺序逐任务实施。
- 权威分类只允许 `unknown | type_i | type_ii`；界面和中文资料只允许“未知类别 / I 类船舶 / II 类船舶”。
- 旧分类词和值只允许出现在 `src/mission/vessel_compat.py`、`src/vis/backend/replay_adapter.py`、对应兼容测试，以及 D/本计划的迁移说明中；新的 API、frame、事件、Prompt、日志和配置不得输出旧值。
- I 类船舶必须满足 `ais_enabled is True`，构造器、命令校验和仿真线程各自防守该不变量。
- `ais_enabled`（是否发射）与 `ais_value_update_enabled`（报文是否有资格改变 V）是两个独立状态，不得共用字段或联动关闭。
- 运行期创建、删除和 `set_ais` 只能由仿真线程应用；API 线程不得直接修改 `Ship`、任务、接力或红方计划。
- 删除船舶必须停止物理实体和新信号、释放运行时引用，但历史观测、证据和审计记录必须保留并按 TTL 自然失效。
- 操作员 frame 可以发布场景船舶类别和 AIS 状态；蓝方 `MissionSnapshot`、任务 Prompt 和接触算法不得读取环境真值类别。
- 红方 LLM 只返回 `heading_offset_deg`、`speed_kn`、`zigzag_heading_deg`、`zigzag_period_min`、`phase_deg`；不得返回位置、轨迹或航路点。
- `InformationUpdatePolicy` 必须成为 `I/S/A/V`、扫描时间、证据和 `information_version` 的唯一权威来源；最终删除旧 `InfoField` 双写。
- 每个任务只提交本任务文件。保留当前工作区已有的未提交测试和文档，不得覆盖、回退或顺带提交。
- 每个任务先运行指定失败测试并确认失败原因，再写实现；提交前运行任务回归。T4、T8、T11、T13 还必须运行完整 `python -m pytest -q`。
- 涉及前端的任务必须运行 `npm --prefix src/vis/frontend run build` 和指定 Playwright；T13 运行完整 Playwright。
- 实施进度记录到 `docs/implementation/2026-09-16-dynamic-vessel-ais-progress.md`，每项写明 commit、测试命令、通过数、失败数、跳过数和偏差。

---

## 0. 文件结构、执行门与依赖

### 新增文件

| 文件 | 唯一职责 |
| --- | --- |
| `src/mission/vessel_compat.py` | 旧配置/日志分类与 AIS 模式的输入映射；不得被新输出路径调用 |
| `src/mission/surveillance_stage.py` | II 类船舶的 `undetected/detected/probing/tracking` 环境侧状态机 |
| `src/vis/backend/replay_adapter.py` | 把旧 frame 归一为新的 I/II 类只读 frame |
| `tests/mission/test_vessel_compat.py` | 旧输入映射与新输出禁止回写测试 |
| `tests/mission/test_surveillance_stage.py` | 阶段优先级、升级、降级、删除测试 |
| `tests/mission/test_dynamic_vessel_lifecycle.py` | 运行期创建/删除/AIS/任务清理集成测试 |
| `tests/mission/test_information_loop.py` | 观测、衰减、版本、触发和过期决策闭环测试 |
| `tests/vis/test_replay_adapter.py` | 旧 frame 到新 frame 的兼容测试 |
| `docs/implementation/2026-09-16-dynamic-vessel-ais-progress.md` | 实施证据账本 |

### 删除文件

| 文件 | 删除原因 |
| --- | --- |
| `src/schedule/info_field.py` | 其扫描/marker 状态与 `InformationUpdatePolicy` 双写，无法提供统一版本 |
| `tests/schedule/test_info_field.py` | 行为迁移到 `test_information_update.py` 与 `test_information_loop.py` |

### 主要修改文件与责任

| 文件组 | 修改责任 |
| --- | --- |
| `configs/ship.yaml`, `src/mission/config.py`, `src/schedule/config_loader.py`, `src/mission/contracts.py` | I/II 类、AIS bool、命令、评估和兼容边界 |
| `src/env/ship.py`, `src/env/ais_signal.py`, `src/env/simulation.py` | 船舶不变量、运行期生命周期、动态红方快照和信息回流 |
| `src/mission/vessel_commands.py`, `src/vis/backend/server.py` | 幂等 create/delete/set_ais 命令和 REST API |
| `src/mission/red_commander.py`, `src/mission/prompts/red_commander.txt` | 动态活跃签名、阶段变化失效、机动参数校验 |
| `src/mission/contact_store.py`, `contact_assessor.py`, `evidence_store.py`, prompts | I/II 接触研判、事件和 AIS 更新资格 |
| `src/mission/information_update.py`, `src/schedule/state_manager.py`, `trigger_manager.py` | 单一信息状态、时间推进、delta 与触发 |
| `src/mission/outcome_evaluator.py`, `strategy_memory.py`, `episode_logger.py`, scripts | I/II 指标、验证报告、日志与 CLI 输出 |
| `src/vis/backend/frame_builder.py`, `src/vis/frontend/src/App.jsx`, `RightSidebar.jsx`, `App.css` | 权威船舶列表、动态数量和 AIS 分段控制 |
| `README.md`, `docs/**/*.md`, `src/**/*.txt` | 资料与 Prompt 的 I/II 术语迁移 |

### 依赖顺序

```text
T1 -> T2 -> T3 -> T4
T2 -> T5 -> T6
T1 -> T7 -> T8
T4 + T6 + T8 -> T9 -> T10
T1 + T4 + T6 -> T11
T1..T11 -> T12 -> T13
```

`src/env/simulation.py` 由 T4、T5、T6、T8 顺序修改，不得并行编辑。`contracts.py` 由 T1 冻结最终字段名，后续任务只能消费，不得发明第二套名称。

### 设计覆盖矩阵

| D 章节 | 实施任务 | 自动化证据 |
| --- | --- | --- |
| §1 总体目标 | T1-T13 | 最终全链路验收、静态门禁与浏览器验收 |
| §2 术语与迁移 | T1、T11、T12、T13 | contract/config/compat 测试与最终 `rg` 门禁 |
| §3 AIS 单一状态 | T2、T4、T8 | AIS generation、动态 lifecycle、information loop |
| §4 运行期命令 | T3、T4 | queue/API/CAS/事务回滚测试 |
| §5 动态红方 LLM | T5、T6 | stage registry、red commander、ship navigation |
| §6 信息闭环 | T7、T8 | information update、trigger、stale version 测试 |
| §7 前端交互 | T9、T10 | frame/replay adapter 与 Playwright |
| §8 API/frame | T3、T9 | server runtime、mixed frame、replay adapter |
| §9 失败处理 | T3、T4、T6、T7 | 422/409、原子删除、模型阻塞、批事务测试 |
| §10 验收 | T11、T13 | 指标、全回归、端到端、浏览器验收 |
| §11 非目标 | T6、T13 | 严格 RedPlan 字段与无轨迹输出断言 |

- [ ] 实施开始前记录：`git status --short`、`git rev-parse HEAD`、`python -m pytest --collect-only -q`、`npm --prefix src/vis/frontend run build`。
- [ ] 使用独立实施分支/worktree；将当前用户改动清单写入进度文档，所有 commit 使用显式文件列表。
- [ ] 确认当前设计 commit 为 `7f87a62`；若 HEAD 已变化，记录新 HEAD，但不得自动回退。

## Task 1: 冻结 I/II 类、AIS 与公共契约

**Files:**
- Create: `src/mission/vessel_compat.py`
- Create: `tests/mission/test_vessel_compat.py`
- Modify: `configs/ship.yaml`
- Modify: `configs/mission.yaml`
- Modify: `configs/sensor.yaml`
- Modify: `src/mission/config.py`
- Modify: `src/schedule/config_loader.py`
- Modify: `src/mission/contracts.py`
- Modify: `tests/mission/test_config.py`
- Modify: `tests/mission/test_contracts.py`
- Modify: `tests/test_runtime_configuration.py`

**Interfaces:**
- Produces: `VesselClass = Literal["unknown", "type_i", "type_ii"]`。
- Produces: `PopulationConfig(total_count:int, type_i_ratio:float, type_ii_ratio:float)`。
- Produces: `ShipConfig.type_ii_ais_on_probability:float`。
- Produces: `VesselCommand(command_id:str, episode_id:str, operation:Literal["create","delete","set_ais"], vessel_id:str|None, expected_revision:int|None, vessel_class:VesselClass|None, position_cells:Vec2|None, ais_enabled:bool|None)`。
- Produces: `normalize_legacy_vessel_class(value:str)->VesselClass`、`normalize_legacy_ais_enabled(value:object)->bool`、`normalize_legacy_ship_config(mapping:Mapping[str,object])->dict`，仅供显式旧输入适配器。

- [ ] **Step 1: 写失败契约和配置测试**

```python
def test_new_runtime_contract_uses_only_type_i_and_type_ii():
    assert get_args(VesselClass) == ("unknown", "type_i", "type_ii")
    assert PopulationConfig(8, 0.625, 0.375).allocate() == {
        "type_i": 5, "type_ii": 3,
    }

@pytest.mark.parametrize("value, expected", [
    ("civilian", "type_i"),
    ("research", "type_ii"),
    ("military", "type_ii"),
    ("target", "type_ii"),
])
def test_legacy_class_values_are_normalized_only_at_input(value, expected):
    assert normalize_legacy_vessel_class(value) == expected

def test_new_ship_config_rejects_old_population_keys(tmp_path):
    path = tmp_path / "ship.yaml"
    path.write_text("population: {total_count: 2, civilian_ratio: 1, research_ratio: 0}\n")
    with pytest.raises(ValueError, match="type_i_ratio"):
        ConfigLoader.load(ship_path=path)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/mission/test_vessel_compat.py tests/mission/test_config.py tests/mission/test_contracts.py tests/test_runtime_configuration.py -q`

Expected: FAIL，首个失败来自 `VesselClass`/`type_i_ratio`/`ais_enabled` 不存在，而不是 YAML 或导入错误。

- [ ] **Step 3: 实现最终公共类型与兼容函数**

```python
VesselClass = Literal["unknown", "type_i", "type_ii"]
SurveillanceStage = Literal["undetected", "detected", "probing", "tracking"]

@dataclass(frozen=True)
class VesselCommand:
    command_id: str
    episode_id: str
    operation: Literal["create", "delete", "set_ais"]
    vessel_id: str | None = None
    expected_revision: int | None = None
    vessel_class: VesselClass | None = None
    position_cells: Vec2 | None = None
    ais_enabled: bool | None = None

_LEGACY_CLASS = {
    "civilian": "type_i",
    "research": "type_ii",
    "military": "type_ii",
    "target": "type_ii",
}

def normalize_legacy_vessel_class(value: str) -> VesselClass:
    normalized = _LEGACY_CLASS.get(value, value)
    if normalized not in {"unknown", "type_i", "type_ii"}:
        raise ValueError(f"unsupported vessel class: {value}")
    return normalized

def normalize_legacy_ais_enabled(value: object) -> bool:
    if type(value) is bool:
        return value
    if value == "civilian":
        return True
    if value == "silent":
        return False
    raise ValueError("unsupported legacy AIS state")

def normalize_legacy_ship_config(mapping: Mapping[str, object]) -> dict:
    result = deepcopy(dict(mapping))
    population = dict(result.get("population", {}))
    if "civilian_ratio" in population:
        population["type_i_ratio"] = population.pop("civilian_ratio")
    if "research_ratio" in population:
        population["type_ii_ratio"] = population.pop("research_ratio")
    if "target_ais_on_probability" in result:
        result["type_ii_ais_on_probability"] = result.pop("target_ais_on_probability")
    result["population"] = population
    return result
```

定义 `VesselClass` 为最终权威类型，并给 `Assessment`/`ContactSnapshot` 增加 canonical `vessel_class` 读取面。为保持中间提交可运行，旧构造参数只作为 `InitVar` 输入适配，旧读取名只作为无 setter 的 deprecated property；序列化新对象时必须过滤这些适配名。T11 迁移全部消费者，T13 删除适配。`AisUpdateState.reason` 使用 `unclassified | confirmed_type_i`，证据 kind 将旧 II 类研判名称改为 `type_ii_assessment`。

- [ ] **Step 4: 迁移配置键并保持比例分配确定性**

```yaml
population:
  total_count: 8
  type_i_ratio: 0.625
  type_ii_ratio: 0.375
type_ii_ais_on_probability: 0.5
```

`allocate_population()` 的顺序固定为 `("type_i", "type_ii")`，最大余数同分时优先 I 类。`SHIP_RNG_STREAMS` 改为 `("ship_class", "ship_ais_enabled")`。标准 `ConfigLoader.load()` 拒绝旧键；显式 `ConfigLoader.load_legacy_ship_config(path)` 先调用 `normalize_legacy_ship_config()` 再进入同一个严格 dataclass loader。

- [ ] **Step 5: 验证 GREEN 并提交**

Run: `python -m pytest tests/mission/test_vessel_compat.py tests/mission/test_config.py tests/mission/test_contracts.py tests/test_runtime_configuration.py -q`

Expected: PASS；序列化新类型时不存在旧分类值。

```bash
git add configs/ship.yaml configs/mission.yaml configs/sensor.yaml \
  src/mission/config.py src/schedule/config_loader.py \
  src/mission/contracts.py src/mission/vessel_compat.py \
  tests/mission/test_vessel_compat.py tests/mission/test_config.py \
  tests/mission/test_contracts.py tests/test_runtime_configuration.py
git commit -m "refactor: define type I and type II vessel contracts"
```

## Task 2: 建立 Ship 的 AIS 单一状态源并迁移船舶生成

**Files:**
- Modify: `src/env/ship.py`
- Modify: `src/env/ais_signal.py`
- Modify: `src/env/emitter.py`
- Modify: `tests/mission/test_ship_population.py`
- Modify: `tests/mission/test_ais_generation.py`
- Modify: `tests/env/test_vessel_activity.py`
- Modify: `tests/env/test_emitter.py`
- Modify: `tests/mission/test_visibility.py`

**Interfaces:**
- Consumes: T1 `VesselClass`、新 `PopulationConfig`。
- Produces: `Ship.vessel_class: VesselClass`、`Ship.ais_enabled: bool`、`Ship.set_ais_enabled(enabled:bool)->None`。
- Produces: `generate_ais_signal(ship, timestamp)->AISSignal|None` 只读取 `ais_enabled`。

- [ ] **Step 1: 写 AIS 不变量和生成测试**

```python
def test_type_i_cannot_disable_ais(type_i_ship):
    with pytest.raises(ValueError, match="type_i_ais_required"):
        type_i_ship.set_ais_enabled(False)
    assert type_i_ship.ais_enabled is True

def test_type_ii_signal_follows_runtime_boolean(type_ii_ship):
    type_ii_ship.set_ais_enabled(False)
    assert generate_ais_signal(type_ii_ship, 1.0) is None
    type_ii_ship.set_ais_enabled(True)
    assert generate_ais_signal(type_ii_ship, 1.1) is not None
```

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/mission/test_ship_population.py tests/mission/test_ais_generation.py tests/env/test_vessel_activity.py tests/env/test_emitter.py tests/mission/test_visibility.py -q`

Expected: FAIL，原因是 `ais_enabled`/`type_i`/`type_ii` 尚未接入实体。

- [ ] **Step 3: 替换 ShipTruth 和 AIS 模式**

```python
@dataclass(frozen=True)
class ShipTruth:
    ship_id: str
    vessel_class: VesselClass
    ais_enabled: bool
    normal_route: tuple[Pose, ...]
    activity_schedule: tuple[tuple[float, float], ...] = ()

def set_ais_enabled(self, enabled: bool) -> None:
    if type(enabled) is not bool:
        raise TypeError("ais_enabled must be bool")
    if self.vessel_class == "type_i" and not enabled:
        raise ValueError("type_i_ais_required")
    self.truth = replace(self.truth, ais_enabled=enabled)
```

删除业务逻辑对 `truth_identity`、`actual_military`、`is_military` 与 `ais_mode` 的消费；为保持中间 commit 全回归可运行，T2 仅保留由 `vessel_class/ais_enabled` 计算的只读 deprecated property，T13 删除。I 类没有活动日程和雷达发射器；II 类保留活动日程、survey route 与发射器。`generate_ais_signal()` 第一行使用 `if not ship.ais_enabled: return None`。

- [ ] **Step 4: 迁移种群分配与独立 RNG**

```python
counts = allocate_population(population.total_count, {
    "type_i": population.type_i_ratio,
    "type_ii": population.type_ii_ratio,
})
class_slots = set(shuffled_slots[:counts["type_ii"]])
vessel_class = "type_ii" if index in class_slots else "type_i"
ais_enabled = (
    True if vessel_class == "type_i"
    else ais_rng.random() < config.ship.type_ii_ais_on_probability
)
```

测试同 seed 同种群、I 类始终发射、II 类 Bernoulli 极值、切换类别但固定 AIS/运动输入不会改变 AIS 可见字段。

- [ ] **Step 5: 验证并提交**

Run: `python -m pytest tests/mission/test_ship_population.py tests/mission/test_ais_generation.py tests/env/test_vessel_activity.py tests/env/test_emitter.py tests/mission/test_visibility.py -q`

Expected: PASS。

```bash
git add src/env/ship.py src/env/ais_signal.py src/env/emitter.py \
  tests/mission/test_ship_population.py tests/mission/test_ais_generation.py \
  tests/env/test_vessel_activity.py tests/env/test_emitter.py tests/mission/test_visibility.py
git commit -m "refactor: make AIS a runtime vessel state"
```

## Task 3: 扩展船舶命令队列和 REST 契约

**Files:**
- Modify: `src/mission/vessel_commands.py`
- Modify: `src/vis/backend/server.py`
- Modify: `tests/mission/test_vessel_commands.py`
- Modify: `tests/env/test_server_runtime.py`

**Interfaces:**
- Consumes: T1 `VesselCommand`。
- Produces: `PATCH /api/vessels/{vessel_id}/ais`。
- Produces: `SimulationEngine.vessel_mutation_allowed: bool`，live 且 episode 未 finished 时为真。

- [ ] **Step 1: 写命令组合和 API 失败测试**

```python
def test_set_ais_requires_only_id_revision_and_boolean():
    command = VesselCommand("a-1", "episode-1", "set_ais",
                            "Ship-2", 4, None, None, False)
    assert VesselCommandQueue().enqueue(command).status == "queued"

@pytest.mark.parametrize("operation", ["create", "delete"])
def test_create_and_delete_remain_available_after_first_step(engine, operation):
    engine.step()
    assert engine.vessel_mutation_allowed is True
```

API 测试断言 PATCH body 只允许 `episode_id,command_id,expected_revision,ais_enabled`，bool 以外返回 422，回放返回 `replay_read_only`，finished episode 返回 `mutation_closed`。

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/mission/test_vessel_commands.py tests/env/test_server_runtime.py -q`

Expected: FAIL，原因是 `set_ais` 和运行期 mutation 尚不存在。

- [ ] **Step 3: 实现精确命令校验**

```python
if command.operation == "set_ais":
    if (not command.vessel_id
            or not _positive_revision(command.expected_revision)
            or type(command.ais_enabled) is not bool
            or command.vessel_class is not None
            or command.position_cells is not None):
        raise ValueError("set_ais requires vessel_id, revision, and ais_enabled")
```

`_payload()` 必须包含 `ais_enabled`，保证幂等 hash 能区分开/关。create 要求 `vessel_class in {type_i,type_ii}` 且 `ais_enabled is None`；delete 要求 AIS 字段为空。

- [ ] **Step 4: 新增 PATCH 并移除初始化窗口 API 限制**

```python
@app.patch("/api/vessels/{vessel_id}/ais")
async def set_vessel_ais(vessel_id: str, request: Request):
    body, error = await _request_object(request)
    if error is not None:
        return error
    allowed = {"episode_id", "command_id", "expected_revision", "ais_enabled"}
    validation_error = _validate_body(body, allowed, allowed)
    if validation_error is not None:
        return validation_error
    write_error = _vessel_write_error(app, body["episode_id"])
    if write_error is not None:
        return write_error
    if (isinstance(body["expected_revision"], bool)
            or not isinstance(body["expected_revision"], int)
            or body["expected_revision"] < 1
            or type(body["ais_enabled"]) is not bool):
        return _api_error("invalid_request", "invalid AIS update", 422)
    command = VesselCommand(
        body["command_id"], body["episode_id"], "set_ais", vessel_id,
        body["expected_revision"], None, None, body["ais_enabled"],
    )
    result = app.state.engine.vessel_commands.enqueue(command)
    return JSONResponse(_vessel_result_payload(result), status_code=202)
```

POST/DELETE/PATCH 和 `/api/scenario/vessels` 都检查 `vessel_mutation_allowed`，不再检查 `clock.time == 0`。HTTP 层不预判 I/II 业务规则，最终规则由仿真线程执行。

- [ ] **Step 5: 验证并提交**

Run: `python -m pytest tests/mission/test_vessel_commands.py tests/env/test_server_runtime.py -q`

Expected: PASS。

```bash
git add src/mission/vessel_commands.py src/vis/backend/server.py \
  tests/mission/test_vessel_commands.py tests/env/test_server_runtime.py
git commit -m "feat: accept runtime vessel and AIS commands"
```

## Task 4: 原子实现运行期创建、删除和 AIS 切换

**Files:**
- Create: `tests/mission/test_dynamic_vessel_lifecycle.py`
- Modify: `src/env/simulation.py`
- Modify: `src/mission/handoff.py`
- Modify: `src/mission/red_commander.py`
- Modify: `tests/mission/test_handoff.py`
- Modify: `tests/env/test_simulation_integration.py`
- Modify: `tests/env/test_vessel_boundary.py`

**Interfaces:**
- Produces: `SimulationEngine.scenario_vessels()->tuple[dict,...]` 始终可读。
- Produces: `HandoffManager.fail_for_contact(contact_id, at_min, reason)->tuple[HandoffAttempt,...]`。
- Produces event: `vessel_created`、`vessel_removed`、`ais_transmission_changed`。
- Test helper contract: `enqueue_and_step(engine, command)` 先调用 `engine.vessel_commands.enqueue(command)`，再调用一次 `engine.step()`，最后用 `engine.vessel_command_result(command.command_id)` 返回终态；`apply_create/apply_ais/apply_delete` 只负责构造带唯一 `command_id` 和当前 `episode_id` 的 `VesselCommand`，并委托给该 helper，不得绕过队列直接调用私有 mutation 方法。

- [ ] **Step 1: 写运行期事务测试**

```python
def test_runtime_create_toggle_delete_is_atomic(engine):
    engine.step()
    created = apply_create(engine, vessel_class="type_ii", position=(12.5, 8.5))
    assert created.status == "applied"
    changed = apply_ais(engine, created.vessel_id, created.revision, False)
    assert changed.status == "applied"
    removed = apply_delete(engine, created.vessel_id, changed.revision)
    assert removed.status == "applied"
    assert created.vessel_id not in {ship.id for ship in engine.ships}
    assert created.vessel_id not in engine._ship_position_history
    assert created.vessel_id not in engine._emitter_track_ids
```

增加测试：I 类关闭返回 `type_i_ais_required` 且 revision 不变；旧 revision 返回 `revision_conflict`；删除 active probe/track/handoff 后 UAV 释放、任务记录结束、历史 evidence 仍在；非法创建不改变任何集合；50 seeds 动态创建后 480 steps 不越界。

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/mission/test_dynamic_vessel_lifecycle.py tests/mission/test_handoff.py tests/env/test_simulation_integration.py tests/env/test_vessel_boundary.py -q`

Expected: FAIL，旧实现因 `editing_closed`、无 `set_ais` 或残留引用失败。

- [ ] **Step 3: 在 step 边界实现命令分派**

```python
def _apply_set_ais(self, command: VesselCommand) -> VesselCommandResult:
    ship = self._require_vessel_revision(command.vessel_id, command.expected_revision)
    ship.set_ais_enabled(command.ais_enabled)
    revision = command.expected_revision + 1
    self._vessel_revisions[ship.id] = revision
    if command.ais_enabled:
        self._ais_force_refresh_ids.add(ship.id)
    self.allocator.trigger_manager.notify_event(
        "ais_transmission_changed", time=self.clock.time,
        vessel_id=ship.id, ais_enabled=command.ais_enabled,
    )
    return VesselCommandResult(command.command_id, "applied", ship.id, revision)
```

删除 `_editing_allowed` 与首步锁定。`vessel_mutation_allowed` 由 live engine lifecycle 决定；`paused_model` 仍可在下一次 `step()`/retry 边界应用命令，`finished` 拒绝。

- [ ] **Step 4: 实现删除预检和无失败提交段**

在改变集合前先解析：船舶、revision、关联 contact IDs、UAV、task records、handoff IDs、emitter ID 和红方状态。预检完成后按固定顺序提交：释放任务 -> fail handoff -> 清理 evaluator link/runtime indexes -> 移除实体 -> 触发事件。提交段不得调用会因输入校验抛错的 API。

```python
@dataclass(frozen=True)
class _VesselRemovalPlan:
    vessel_id: str
    revision: int
    contact_ids: tuple[str, ...]
    assigned_uav_ids: tuple[str, ...]
    handoff_ids: tuple[str, ...]

def fail_for_contact(self, contact_id: str, at_min: float, reason: str):
    failed = []
    for attempt in self.attempts():
        if attempt.contact_id == contact_id and attempt.state in {"required", "pending"}:
            failed.append(self._fail(attempt.handoff_id, at_min, reason))
    return tuple(failed)
```

历史 ContactStore sample、EvidenceStore record、episode log 不删除；contact 以 `vessel_removed` reason release，随后自然 TTL 失效。

T4 同时给 `ThreatGate` 和 `RedCommander` 增加最小 `remove_ship(ship_id)->None`：删除该船 gate state；若当前 installation 含该船则整体 retire。T6 再把该接口扩展为阶段签名失效，不允许 T4 留下已删除船舶计划等待后续任务修复。

- [ ] **Step 5: 验证完整后端并提交**

Run: `python -m pytest tests/mission/test_dynamic_vessel_lifecycle.py tests/mission/test_handoff.py tests/env/test_simulation_integration.py tests/env/test_vessel_boundary.py -q`

Run: `python -m pytest -q`

Expected: 全部 PASS，且无 `editing_closed` 初始化窗口假设。

```bash
git add src/env/simulation.py src/mission/handoff.py src/mission/red_commander.py \
  tests/mission/test_dynamic_vessel_lifecycle.py tests/mission/test_handoff.py \
  tests/env/test_simulation_integration.py tests/env/test_vessel_boundary.py
git commit -m "feat: manage vessels atomically during simulation"
```

## Task 5: 建立 UAV 递进侦察阶段状态机

**Files:**
- Create: `src/mission/surveillance_stage.py`
- Create: `tests/mission/test_surveillance_stage.py`
- Modify: `src/env/simulation.py`
- Modify: `tests/mission/test_probe_session.py`
- Modify: `tests/mission/test_contact_lifecycle.py`

**Interfaces:**
- Produces: `SurveillanceState(ship_id, stage, revision, changed_at_min, cause_id)`。
- Produces: `SurveillanceStageRegistry.set_fact(ship_id, source, active, now_min, cause_id)->SurveillanceState|None`。
- Produces: `SurveillanceStageRegistry.remove(ship_id)->None`、`snapshot(ship_id)`。

- [ ] **Step 1: 写阶段优先级和降级测试**

```python
def test_stage_is_derived_from_active_uav_facts():
    stages = SurveillanceStageRegistry()
    stages.register("Ship-2", "type_ii", 0.0)
    assert stages.snapshot("Ship-2").stage == "undetected"
    stages.set_fact("Ship-2", "sar", True, 1.0, "OBS-1")
    assert stages.snapshot("Ship-2").stage == "detected"
    stages.set_fact("Ship-2", "probe", True, 2.0, "P-1")
    assert stages.snapshot("Ship-2").stage == "probing"
    stages.set_fact("Ship-2", "eo_lock", True, 3.0, "EO-1")
    assert stages.snapshot("Ship-2").stage == "tracking"
    stages.set_fact("Ship-2", "eo_lock", False, 4.0, "EO-LOST")
    assert stages.snapshot("Ship-2").stage == "probing"
```

测试 I 类永远保持 `undetected` 且不进入红方活跃集合；AIS fact 不被接受为阶段来源；重复 fact 不增加 revision；删除后 snapshot KeyError。

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/mission/test_surveillance_stage.py tests/mission/test_probe_session.py tests/mission/test_contact_lifecycle.py -q`

Expected: FAIL，状态机尚不存在。

- [ ] **Step 3: 实现来源集合推导而非命令式跳转**

```python
_PRIORITY = {"undetected": 0, "detected": 1, "probing": 2, "tracking": 3}
_SOURCE_STAGE = {
    "sar": "detected",
    "passive": "detected",
    "probe": "probing",
    "eo_lock": "tracking",
}

def _derived_stage(active_sources: set[str]) -> SurveillanceStage:
    return max(
        (_SOURCE_STAGE[source] for source in active_sources),
        key=_PRIORITY.__getitem__,
        default="undetected",
    )
```

注册表保存每船 active source/cause，变更后才增加 revision。状态只属于环境/红方，不写入蓝方 ContactSnapshot 或 MissionSnapshot。

- [ ] **Step 4: 接入真实执行事实**

- `_handle_detection()` 的 SAR 成功关联后设置 `sar`。
- 被动观测成功关联 emitter 对应 II 类船舶后设置 `passive`。
- probe assignment/结束分别设置/清除 `probe`。
- 有效 EO link 建立/丢失分别设置/清除 `eo_lock`。
- contact lost、任务释放、船舶删除必须清理对应 source。

所有阶段变更发布 `surveillance_stage_changed`，载荷含 `vessel_id,previous_stage,stage,revision,cause_id`。

- [ ] **Step 5: 验证并提交**

Run: `python -m pytest tests/mission/test_surveillance_stage.py tests/mission/test_probe_session.py tests/mission/test_contact_lifecycle.py -q`

Expected: PASS。

```bash
git add src/mission/surveillance_stage.py src/env/simulation.py \
  tests/mission/test_surveillance_stage.py tests/mission/test_probe_session.py \
  tests/mission/test_contact_lifecycle.py
git commit -m "feat: track progressive UAV surveillance stages"
```

## Task 6: 让红方 LLM 管理动态 II 类活跃集合

**Files:**
- Modify: `src/mission/red_commander.py`
- Modify: `src/mission/prompts/red_commander.txt`
- Modify: `src/env/simulation.py`
- Modify: `src/mission/contracts.py`
- Modify: `tests/mission/test_red_commander.py`
- Modify: `tests/mission/test_ship_navigation.py`

**Interfaces:**
- Consumes: T5 `SurveillanceState`。
- Produces: `RedShipSnapshot(ship_id,vessel_class,surveillance_stage,position_cells,heading_deg,speed_kn,normal_tangent_deg,ais_enabled)`。
- Produces: `RedSnapshot.active_signature: tuple[tuple[str,SurveillanceStage],...]`。
- Produces: `RedPlanInstallation.active_signature`，签名不匹配时不可复用。

- [ ] **Step 1: 写动态集合和参数边界测试**

```python
def test_stage_change_bypasses_periodic_reuse(commander, snapshots):
    commander.decide(snapshots.detected)
    second = commander.decide(snapshots.probing_same_time_window)
    assert second.snapshot_id == snapshots.probing_same_time_window.snapshot_id
    assert commander.gateway.call_count == 2

def test_response_must_exactly_cover_dynamic_signature(commander, snapshot):
    commander.gateway.reply(commands=[valid_command("Ship-1")])
    with pytest.raises(RedDecisionBlocked, match="active_ship_ids"):
        commander.decide(replace(snapshot, active_signature=(
            ("Ship-1", "tracking"), ("Ship-2", "detected"))))
```

测试新增 II 类进入、删除退出、I 类永不进入、`undetected` 不进入、stage 不变时 3 分钟周期复用、集合变化时旧计划不可复用、LLM 输出不含轨迹字段。

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/mission/test_red_commander.py tests/mission/test_ship_navigation.py -q`

Expected: FAIL，旧 snapshot 使用固定 `identity/gate_state` 且 installation 不含阶段签名。

- [ ] **Step 3: 重构 snapshot 与活跃判定**

```python
active_signature = tuple(sorted(
    (ship.id, stages.snapshot(ship.id).stage)
    for ship in self.ships
    if ship.vessel_class == "type_ii"
    and stages.snapshot(ship.id).stage in {"detected", "probing", "tracking"}
))
active_ship_ids = tuple(ship_id for ship_id, _ in active_signature)
```

Prompt 明确阶段含义、只返回五个参数、commands 精确覆盖 active IDs。`_validate_plan()` 继续严格字段等于 dataclass fields、有限数、上下界、最小机动约束和 2 秒 deadline。

- [ ] **Step 4: 实现计划复用和删除失效**

```python
can_reuse = (
    installed is not None
    and installed.active_signature == snapshot.active_signature
    and snapshot.sim_time_min < installed.expires_at_min
)
if signature_changed:
    self._retire_installation()
```

LLM 失败时只有 `can_reuse` 才返回旧计划；否则抛 `RedDecisionBlocked`，仿真进入 `paused_model`。安装命令只传给匹配的 II 类实体；I 类及 undetected II 类调用 `navigator.install(None, now)`，继续规则航行。

- [ ] **Step 5: 验证完整后端并提交**

Run: `python -m pytest tests/mission/test_red_commander.py tests/mission/test_ship_navigation.py tests/mission/test_dynamic_vessel_lifecycle.py -q`

Run: `python -m pytest -q`

Expected: 全部 PASS；测试明确 LLM 输出是参数而非轨迹。

```bash
git add src/mission/red_commander.py src/mission/prompts/red_commander.txt \
  src/env/simulation.py src/mission/contracts.py \
  tests/mission/test_red_commander.py tests/mission/test_ship_navigation.py
git commit -m "feat: replan dynamic type II vessel maneuvers"
```

## Task 7: 将 InformationUpdatePolicy 变为唯一信息状态

**Files:**
- Delete: `src/schedule/info_field.py`
- Delete: `tests/schedule/test_info_field.py`
- Modify: `src/mission/information_update.py`
- Modify: `src/mission/evidence_store.py`
- Modify: `src/mission/contracts.py`
- Modify: `src/schedule/state_manager.py`
- Modify: `tests/mission/test_information_update.py`
- Modify: `tests/mission/test_evidence_store.py`
- Modify: `tests/schedule/test_state_manager.py`

**Interfaces:**
- Produces: `InformationUpdatePolicy.apply_batch(facts,now_min)->InfoFieldDelta|None`。
- Produces: `InformationUpdatePolicy.advance_time(now_min)->InfoFieldDelta|None`。
- Produces: `InformationUpdatePolicy.last_scan_time`、`info_matrix(now)`、`value_matrix(now)`。
- Evidence kind 新增 `track_loss`，旧 marker 不再直接写 V。

- [ ] **Step 1: 写扫描、衰减、TTL 与单一来源测试**

```python
def test_decay_accumulates_until_material_delta_then_versions(config):
    policy = InformationUpdatePolicy(config)
    policy.apply_batch([ScanRefresh((1, 1, 2, 2), "search")], 0.0)
    version = policy.version
    assert policy.advance_time(0.1) is None
    delta = policy.advance_time(10.0)
    assert delta.version == version + 1
    assert delta.max_abs_value_delta >= 0.05

def test_expiry_emits_dirty_bbox_and_reason(policy, evidence):
    policy.apply_batch([evidence], 0.0)
    delta = policy.advance_time(evidence.expires_at_min)
    assert "evidence_expired" in delta.reason_codes
    assert evidence.evidence_id in delta.cause_evidence_ids
```

测试相同步骤分割结果一致、候选阈值穿越即使 delta<0.05 也版本化、相同时间 advance 幂等、scan 返回 dirty bbox、旧 marker 路径不再改变 V。

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/mission/test_information_update.py tests/mission/test_evidence_store.py tests/schedule/test_state_manager.py -q`

Expected: FAIL，`advance_time` 不存在且 StateManager 仍取两个矩阵最大值。

- [ ] **Step 3: 实现累计基线与时间 delta**

```python
def _commit_delta(self, before, after, now_min, reasons, causes, urgent=False):
    changed = np.abs(after - before) > 1e-12
    crossed = (before < self.config.grid.candidate_value_threshold) != (
        after < self.config.grid.candidate_value_threshold)
    material = float(np.max(np.abs(after - before))) >= 0.05
    if not (material or np.any(crossed) or "evidence_expired" in reasons):
        return None
    bbox = _mask_bbox(changed | crossed, self.cols, self.rows)
    self._version += 1
    self._committed_value = after.copy()
    self._committed_at_min = float(now_min)
    return InfoFieldDelta(
        previous_version=self._version - 1,
        version=self._version,
        changed_bbox=bbox,
        max_abs_value_delta=float(np.max(np.abs(after - before))),
        value_changed=bool(np.any(changed)),
        crossed_candidate_threshold=bool(np.any(crossed)),
        urgent=bool(urgent),
        reason_codes=tuple(dict.fromkeys(reasons)),
        cause_evidence_ids=tuple(dict.fromkeys(causes)),
    )

def advance_time(self, now_min: float):
    after = self.matrices(now_min)[3]
    expired = self.evidence_store.expired_ids_since(
        self._committed_at_min, now_min)
    return self._commit_delta(
        self._committed_value, after, now_min,
        ("evidence_expired",) if expired else ("time_decay",), expired,
    )
```

`apply_batch()` 先原子修改 scan/evidence 状态，再把当前矩阵与 `_committed_value` 比较；只有实际发布 delta 时才更新 committed baseline。未发布的小变化继续相对旧 baseline 累计。证据过期索引必须幂等，不得重复报告同一 expiry。

- [ ] **Step 4: 删除双写并迁移 StateManager facade**

`StateManager.scan_cell/scan_bbox` 返回 policy delta；`get_info_matrix/get_value_matrix/get_last_scan_matrix/get_coverage_stats` 全部只读 policy。`release_track_region(create_marker=True)` 只创建 UI marker；要影响 V 时由调用方提交 `handoff` 或 `track_loss` EvidenceRecord。

```python
def get_value_matrix(self):
    return self.information_policy.value_matrix(self.current_time)

def scan_cell(self, coord, current_time, is_track=False):
    return self.information_policy.apply_batch([
        ScanRefresh((coord.col, coord.row, coord.col + 1, coord.row + 1),
                    "track" if is_track else "search")
    ], current_time)
```

- [ ] **Step 5: 验证并提交**

Run: `python -m pytest tests/mission/test_information_update.py tests/mission/test_evidence_store.py tests/schedule/test_state_manager.py -q`

Expected: PASS；`rg "info_field" src tests` 不再发现运行时对象引用。

```bash
git add src/mission/information_update.py src/mission/evidence_store.py \
  src/mission/contracts.py src/schedule/state_manager.py \
  tests/mission/test_information_update.py tests/mission/test_evidence_store.py \
  tests/schedule/test_state_manager.py
git rm src/schedule/info_field.py tests/schedule/test_info_field.py
git commit -m "refactor: unify versioned information state"
```

## Task 8: 接通所有信息 delta、AIS 状态和调度触发

**Files:**
- Create: `tests/mission/test_information_loop.py`
- Modify: `src/env/simulation.py`
- Modify: `src/schedule/trigger_manager.py`
- Modify: `src/schedule/task_allocator.py`
- Modify: `tests/schedule/test_trigger_manager.py`
- Modify: `tests/mission/test_maritime_acceptance.py`
- Modify: `tests/mission/test_simulation_flow.py`

**Interfaces:**
- Consumes: T7 所有 delta。
- Produces helper: `SimulationEngine._publish_information_delta(delta, at_min)->None`。
- Trigger heavy events: `ais_transmission_changed`、`surveillance_stage_changed`、material/urgent `information_delta`。
- Test fixture contract: `force_one_valid_sar_cell(engine)` 从 `engine.allocator.sm.searchable_mask` 选择第一个可搜索 cell，并通过正式 SAR observation 入口制造一次 scan；`seed_scanned_information(engine, at_min)` 用 `StateManager.scan_cell()` 建立非零 I/S/A/V 基线并保存其 version；`advance_without_new_observations(engine, until_material_decay=True)` 只推进 clock 与 `InformationUpdatePolicy.advance_time()`，直到第一次 material delta，禁止直接改矩阵或调用 TriggerManager 私有字段。

- [ ] **Step 1: 写端到端触发失败测试**

```python
def test_sar_scan_delta_reaches_trigger_manager(engine):
    force_one_valid_sar_cell(engine)
    engine.step()
    events = engine.allocator.trigger_manager.pending_events_for_test()
    assert any(e["type"] == "information_delta" for e in events)

def test_time_decay_can_trigger_before_periodic_cycle(engine):
    seed_scanned_information(engine, at_min=0.0)
    advance_without_new_observations(engine, until_material_decay=True)
    assert engine.last_result["trigger_type"] == "heavy"
    assert engine.last_result["trigger_information_version"] > 0
```

另测 AIS off 不生成新 evidence、旧 evidence 仍衰减；AIS on 下一步生成新报文；普通小扫描不立即 heavy；过期 selection 被 `stale_information_version` 拒绝。

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/mission/test_information_loop.py tests/schedule/test_trigger_manager.py tests/mission/test_maritime_acceptance.py tests/mission/test_simulation_flow.py -q`

Expected: FAIL，扫描 delta 被丢弃或时间衰减无事件。

- [ ] **Step 3: 集中发布 delta**

```python
def _publish_information_delta(self, delta, at_min):
    if delta is not None:
        self.allocator.trigger_manager.notify_information_delta(delta, time=at_min)

# SAR/EO scan
delta = sm.scan_cell(cell, current_time, is_track=is_track)
self._publish_information_delta(delta, current_time)

# 每步传感器事实处理完、调度快照冻结前
self._publish_information_delta(
    sm.information_policy.advance_time(current_time), current_time)
```

AIS 开启命令把船 ID 放入 `_ais_force_refresh_ids`；`_refresh_ais_signals()` 对这些 ID 忽略全局 interval 一次，生成成功后移除。关闭船不生成 signal，也不得向 evasion detector 添加伪造断点。

- [ ] **Step 4: 调整 step 顺序和触发规则**

固定顺序：应用命令 -> 红方集合检查 -> tick -> 船舶/传感器/AIS -> contact/probe -> `advance_time` -> StateManager 同步 -> TriggerManager/蓝方调度 -> evaluation。这样同帧信息版本进入同一调度快照。

`TriggerManager` 将 AIS 开关、阶段变化作为 heavy；`information_delta` 在 urgent、`max_abs>=0.05`、阈值穿越或 `evidence_expired` 时 heavy。事件去重键包含 `information_version`，不能因同 UAV 的 5 分钟去重吞掉新版本。

- [ ] **Step 5: 验证完整后端并提交**

Run: `python -m pytest tests/mission/test_information_loop.py tests/schedule/test_trigger_manager.py tests/mission/test_maritime_acceptance.py tests/mission/test_simulation_flow.py -q`

Run: `python -m pytest -q`

Expected: 全部 PASS；测试证明衰减不依赖 30 分钟周期才能被发现。

```bash
git add src/env/simulation.py src/schedule/trigger_manager.py \
  src/schedule/task_allocator.py tests/mission/test_information_loop.py \
  tests/schedule/test_trigger_manager.py tests/mission/test_maritime_acceptance.py \
  tests/mission/test_simulation_flow.py
git commit -m "feat: close the information update trigger loop"
```

## Task 9: 发布运行期权威 frame 并兼容旧回放

**Files:**
- Create: `src/vis/backend/replay_adapter.py`
- Create: `tests/vis/test_replay_adapter.py`
- Modify: `src/vis/backend/frame_builder.py`
- Modify: `src/vis/backend/server.py`
- Modify: `src/schedule/state_manager.py`
- Modify: `tests/env/test_mixed_frame.py`
- Modify: `tests/env/test_frame_publisher.py`
- Modify: `tests/env/test_server_runtime.py`

**Interfaces:**
- Produces frame field: `vessel_mutation_allowed: bool`。
- Produces `scenario_vessels[] = {scenario_entity_id,revision,position,vessel_class,ais_enabled,ais_controllable,surveillance_stage}`。
- Produces: `normalize_replay_frame(frame:dict)->dict`。
- Produces: `StateManager.publish_vessel_inventory(items:Iterable[Mapping])->None` 与 `get_vessel_inventory()->tuple[dict,...]`，仅供 operator frame。
- Test helper contract: `build_engine_frame(engine)` 必须调用公共 `build_frame(engine.allocator.sm, cycle=0, config=engine.config, ships=engine.ships, uav_entities=engine.uavs, obstacles=engine.obstacles, bases=engine.bases)`；不得直接拼 frame dict。

- [ ] **Step 1: 写 live frame 与 replay adapter 测试**

```python
def test_live_frame_always_contains_runtime_vessel_inventory(engine):
    engine.step()
    frame = build_engine_frame(engine)
    assert frame["vessel_mutation_allowed"] is True
    assert {item["vessel_class"] for item in frame["scenario_vessels"]} <= {
        "type_i", "type_ii",
    }
    assert all(type(item["ais_enabled"]) is bool for item in frame["scenario_vessels"])

def test_old_replay_is_normalized_without_rewriting_source():
    old = {"scenario_vessels": [{"vessel_class": "research", "ais_mode": "silent"}]}
    new = normalize_replay_frame(old)
    assert new["scenario_vessels"][0]["vessel_class"] == "type_ii"
    assert new["scenario_vessels"][0]["ais_enabled"] is False
    assert old["scenario_vessels"][0]["vessel_class"] == "research"
```

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/vis/test_replay_adapter.py tests/env/test_mixed_frame.py tests/env/test_frame_publisher.py tests/env/test_server_runtime.py -q`

Expected: FAIL，运行后 inventory 为空且旧 replay 未转换。

- [ ] **Step 3: 发布不依赖初始化窗口的 inventory**

```python
def _publish_vessel_inventory(self) -> None:
    items = tuple({
        "scenario_entity_id": ship.id,
        "revision": self._vessel_revisions[ship.id],
        "position": [float(ship.float_position[0]), float(ship.float_position[1])],
        "vessel_class": ship.vessel_class,
        "ais_enabled": ship.ais_enabled,
        "ais_controllable": ship.vessel_class == "type_ii",
        "surveillance_stage": self.surveillance_stages.snapshot(ship.id).stage,
    } for ship in self.ships)
    self.allocator.sm.publish_vessel_inventory(items)

# frame_builder.py
scenario_vessels = list(state.get_vessel_inventory())
```

`StateManager` 深拷贝写入和读取 inventory；任务目录、MissionSnapshot 和蓝方 Prompt 不提供该 getter，防止 operator truth 进入蓝方决策。

`configured_vessel_count` 改名为 `initial_vessel_count`，`actual_vessel_count` 每帧取当前实体数；旧字段只在 replay adapter 输入读取。frame 不把该 inventory 传进 MissionSnapshot。

- [ ] **Step 4: 在 replay API 输出边界归一化**

`/api/replay` 读取每行后调用 `normalize_replay_frame`；adapter 深拷贝输入，缺失 AIS 状态时依据旧字段映射，缺失 stage 时用 `undetected`，并强制 `ais_controllable = vessel_class == type_ii`。新 frame 不经过旧值分支。

- [ ] **Step 5: 验证并提交**

Run: `python -m pytest tests/vis/test_replay_adapter.py tests/env/test_mixed_frame.py tests/env/test_frame_publisher.py tests/env/test_server_runtime.py -q`

Expected: PASS。

```bash
git add src/vis/backend/replay_adapter.py src/vis/backend/frame_builder.py \
  src/vis/backend/server.py src/schedule/state_manager.py \
  tests/vis/test_replay_adapter.py \
  tests/env/test_mixed_frame.py tests/env/test_frame_publisher.py \
  tests/env/test_server_runtime.py
git commit -m "feat: publish dynamic vessel state in live and replay frames"
```

## Task 10: 实现右侧动态数量、删除和 II 类 AIS 控件

**Files:**
- Modify: `src/vis/frontend/src/App.jsx`
- Modify: `src/vis/frontend/src/components/RightSidebar.jsx`
- Modify: `src/vis/frontend/src/components/CanvasMap.jsx`
- Modify: `src/vis/frontend/src/components/BottomDrawer.jsx`
- Modify: `src/vis/frontend/src/components/ContactPanel.jsx`
- Modify: `src/vis/frontend/src/renderer/layers.js`
- Modify: `src/vis/frontend/src/App.css`
- Modify: `src/vis/frontend/tests/mixed-maritime.spec.js`
- Modify: `src/vis/frontend/tests/acceptance.spec.js`

**Interfaces:**
- Consumes: T9 `scenario_vessels`。
- Produces: `submitVesselCommand(request, successMessage)` 统一轮询状态。
- Produces: `handleSetVesselAis(enabled:boolean)`。

- [ ] **Step 1: 先写 Playwright 失败验收**

覆盖：运行 10 分钟后 I/II 放置按钮仍启用；计数从 8 -> 9 -> 8；I 类显示开启且不可关闭；II 类 `[开启|关闭]` 当前态高亮；PATCH body revision 正确；queued 时禁用；成功只等 frame 刷新；409 revision conflict 显示错误；回放全部只读。

```javascript
await page.getByRole("button", { name: "II 类船舶" }).click();
await page.getByRole("button", { name: /scenario-vessel-9/ }).click();
await page.getByRole("button", { name: "关闭 AIS" }).click();
expect(patched).toMatchObject({ expected_revision: 4, ais_enabled: false });
await expect(page.getByRole("button", { name: "关闭 AIS" })).toHaveAttribute("aria-pressed", "true");
```

- [ ] **Step 2: 运行并确认 RED**

Run: `npm --prefix src/vis/frontend run test:acceptance -- tests/mixed-maritime.spec.js`

Expected: FAIL，旧 UI 显示旧类别、运行期禁用且无 AIS 控件。

- [ ] **Step 3: 重构 App 命令提交**

```jsx
const handleSetVesselAis = async (enabled) => {
  const vessel = scenarioVessels.find((item) => item.scenario_entity_id === selectedId);
  if (!vessel || !vessel.ais_controllable || vessel.ais_enabled === enabled) return;
  await submitVesselCommand({
    url: `/api/vessels/${encodeURIComponent(vessel.scenario_entity_id)}/ais`,
    method: "PATCH",
    body: {
      episode_id: frame.episode_id,
      command_id: commandId(),
      expected_revision: vessel.revision,
      ais_enabled: enabled,
    },
  });
};
```

创建/删除权限使用 `mode === "live" && frame.vessel_mutation_allowed`，不再依赖首步时间。命令 applied 后不本地改 AIS；等待 WebSocket 权威 revision/frame。

- [ ] **Step 4: 实现 RightSidebar 分段控件**

使用 lucide `RadioTower`/`RadioTowerOff` 图标；列表行显示 `I 类`/`II 类` 和 AIS 状态点。选中详情中使用稳定宽度的 segmented control：I 类按钮禁用且“开启”高亮，II 类可选，回放禁用。删除按钮运行期保持可用。CSS 复用现有边框/状态色，不新增卡中卡或改变侧栏宽度。

- [ ] **Step 5: build、Playwright、提交**

Run: `npm --prefix src/vis/frontend run build`

Run: `npm --prefix src/vis/frontend run test:acceptance -- tests/mixed-maritime.spec.js`

Expected: build PASS；Playwright PASS；浏览器 console 无 error。

```bash
git add src/vis/frontend/src/App.jsx \
  src/vis/frontend/src/components/RightSidebar.jsx \
  src/vis/frontend/src/components/CanvasMap.jsx \
  src/vis/frontend/src/components/BottomDrawer.jsx \
  src/vis/frontend/src/components/ContactPanel.jsx \
  src/vis/frontend/src/renderer/layers.js src/vis/frontend/src/App.css \
  src/vis/frontend/tests/mixed-maritime.spec.js \
  src/vis/frontend/tests/acceptance.spec.js
git commit -m "feat: control dynamic vessel AIS from the sidebar"
```

## Task 11: 迁移接触研判、评估、日志和策略指标

**Files:**
- Modify: `src/mission/contact_store.py`
- Modify: `src/mission/contact_assessor.py`
- Modify: `src/mission/prompts/contact_assessor.txt`
- Modify: `src/mission/evidence_store.py`
- Modify: `src/mission/outcome_evaluator.py`
- Modify: `src/mission/strategy_memory.py`
- Modify: `src/mission/episode_logger.py`
- Modify: `src/schedule/llm_reviewer.py`
- Modify: `src/schedule/candidate_extractor.py`
- Modify: `src/schedule/trigger_manager.py`
- Modify: `src/control/heuristic/task_flow.py`
- Modify: `scripts/evaluate_goal.py`
- Modify: `scripts/evaluate_mixed_maritime.py`
- Modify: `scripts/validate_strategy_memory.py`
- Modify: `tests/mission/test_contact_store.py`
- Modify: `tests/mission/test_contact_assessor.py`
- Modify: `tests/mission/test_outcome_evaluator.py`
- Modify: `tests/mission/test_episode_logger.py`
- Modify: `tests/mission/test_strategy_validation.py`
- Modify: `tests/mission/test_maritime_alignment_contracts.py`
- Modify: `tests/mission/test_evidence_store.py`
- Modify: `tests/schedule/test_candidate_extractor.py`
- Modify: `tests/control/heuristic/test_task_flow.py`

**Interfaces:**
- Produces metrics: `type_ii_tracking_ratio`、`type_ii_misclassified_as_type_i_ratio`、`type_i_probe_uav_min`、`type_i_probe_cost`、`type_i_recall`、`type_ii_recall`。
- Produces event: `type_i_released`、`type_ii_confirmed`。
- Produces assessment labels: `unknown | type_i | type_ii`。

- [ ] **Step 1: 写新指标和旧日志输入测试**

```python
def test_type_metrics_have_unambiguous_names(outcome):
    assert outcome.type_ii_tracking_ratio == pytest.approx(0.5)
    assert outcome.type_ii_misclassified_as_type_i_ratio == 0.0
    assert outcome.type_i_probe_cost == pytest.approx(
        outcome.type_i_probe_uav_min / max(outcome.total_uav_active_min, 1.0))

def test_new_episode_json_contains_no_old_metric_keys(logger, outcome):
    payload = logger.serialize_outcome(outcome)
    assert "target_tracking_ratio" not in payload
    assert "false_civilian_ratio" not in payload
```

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/mission/test_contact_store.py tests/mission/test_contact_assessor.py tests/mission/test_outcome_evaluator.py tests/mission/test_episode_logger.py tests/mission/test_strategy_validation.py -q`

Expected: FAIL，旧 assessment 和 metric 名仍存在。

- [ ] **Step 3: 完成研判和事件迁移**

Prompt schema 使用 `vessel_class`，模型只能返回 `unknown/type_i/type_ii`。`ContactStore.apply_assessment()` 在 type_i 确认时生成 `type_i_assessed/type_i_released`；type_ii 生成 `type_ii_assessed/type_ii_confirmed`。AIS 更新资格 reason 改为 `confirmed_type_i`。控制事件转换表同步改名。

- [ ] **Step 4: 完成指标与策略报告迁移**

保持 `EpisodeOutcome` 现有字段顺序，只做以下一一对应重命名，并同步 `__post_init__` 数值校验、`asdict()` 输出和所有构造调用：

| 现有字段 | 最终字段 |
| --- | --- |
| `target_tracking_ratio` | `type_ii_tracking_ratio` |
| `false_civilian_ratio` | `type_ii_misclassified_as_type_i_ratio` |
| `civilian_probe_uav_min` | `type_i_probe_uav_min` |
| `civilian_recall` | `type_i_recall` |
| `research_recall` | `type_ii_recall` |
| `civilian_probe_cost` property | `type_i_probe_cost` property |

`type_i_probe_cost` 精确定义为 `type_i_probe_uav_min / max(total_uav_active_min, 1.0)`。

`ValidationReport.false_civilian_delta` 改为 `type_ii_misclassified_as_type_i_delta`，拒绝 reason 改为 `type_ii_misclassification_regression`。旧 outcome JSON 只由兼容读函数映射，所有新 CLI/JSON 输出使用新键。

- [ ] **Step 5: 验证完整后端并提交**

Run: `python -m pytest tests/mission/test_contact_store.py tests/mission/test_contact_assessor.py tests/mission/test_outcome_evaluator.py tests/mission/test_episode_logger.py tests/mission/test_strategy_validation.py -q`

Run: `python -m pytest -q`

Expected: 全部 PASS；新日志无旧分类或旧指标键。

```bash
git add src/mission/contact_store.py src/mission/contact_assessor.py \
  src/mission/prompts/contact_assessor.txt src/mission/evidence_store.py \
  src/mission/outcome_evaluator.py src/mission/strategy_memory.py \
  src/mission/episode_logger.py src/schedule/llm_reviewer.py \
  src/schedule/candidate_extractor.py src/schedule/trigger_manager.py \
  src/control/heuristic/task_flow.py \
  scripts/evaluate_goal.py scripts/evaluate_mixed_maritime.py \
  scripts/validate_strategy_memory.py \
  tests/mission/test_contact_store.py tests/mission/test_contact_assessor.py \
  tests/mission/test_outcome_evaluator.py tests/mission/test_episode_logger.py \
  tests/mission/test_strategy_validation.py \
  tests/mission/test_maritime_alignment_contracts.py \
  tests/mission/test_evidence_store.py \
  tests/schedule/test_candidate_extractor.py \
  tests/control/heuristic/test_task_flow.py
git commit -m "refactor: migrate assessment and metrics to type I and II"
```

提交前必须用 `git diff --cached --name-only` 核对，避免把用户预存的无关测试改动纳入；如 `tests/mission` 中有既存改动，逐文件显式 `git add`，不得使用目录级 add。

## Task 12: 全量迁移界面外资料和历史设计文本

**Files:**
- Modify: `README.md`
- Modify: `docs/2026-09-15-maritime-requirements.md`
- Modify: `docs/GOAL.md`
- Modify: `docs/GOAL2.md`
- Modify: `docs/MIXED_MARITIME_VALIDATION.md`
- Modify: `docs/SYSTEM_PARAMS.md`
- Modify: `docs/VALIDATION.md`
- Modify: `docs/implementation/mixed-maritime-progress.md`
- Modify: `docs/superpowers/plans/2026-08-29-control-strategy-architecture-implementation-plan.md`
- Modify: `docs/superpowers/plans/2026-09-14-mixed-maritime-llm-implementation-plan.md`
- Modify: `docs/superpowers/plans/2026-09-15-maritime-requirements-alignment-implementation-plan.md`
- Modify: `docs/superpowers/specs/2026-07-30-llm-dynamic-task-reallocation-design.md`
- Modify: `docs/superpowers/specs/2026-08-29-control-strategy-architecture-design.md`
- Modify: `docs/superpowers/specs/2026-09-14-mixed-maritime-llm-design.md`
- Modify: `docs/superpowers/specs/2026-09-15-maritime-requirements-alignment-design.md`
- Modify: `src/control/interface.md`

**Interfaces:**
- Consumes: final names from T1/T11。
- Produces: 全仓资料只描述 I 类/II 类；历史迁移例外有明确标签。

- [ ] **Step 1: 生成基线清单并分类**

Run:

```bash
rg -n -i 'civilian|research|military|民船|民用船舶|科考船|舰船' \
  README.md docs src configs scripts tests
```

将命中分为：分类语义（必须迁移）、兼容映射（允许）、英文通用语义或第三方文本（逐项判断）。不得盲目把 Python 标识符内的字符串替换成中文。

- [ ] **Step 2: 按固定语义表修改资料**

| 旧语义 | 新语义 |
| --- | --- |
| 民用船舶/民船 | I 类船舶 |
| 科考船舶/舰船/军用船舶 | II 类船舶 |
| civilian class/value | `type_i` |
| research/military/target class/value | `type_ii` |
| civilian release | I 类确认后释放 |
| target tracking metric | II 类持续跟踪指标 |
| false civilian metric | II 类误判为 I 类指标 |

历史设计和计划中的代码示例也改为当前接口，并在顶部增加“术语已按 2026-09-16 统一”的短注；不能保留会误导实施者的旧 schema。D 与本计划中的兼容映射保持原样。

- [ ] **Step 3: 更新测试名、事件文案和 Prompt**

测试函数名、fixture、错误消息、UI aria-label、日志说明和 Prompt 已分别归属 T1-T11；T12 的扫描是验收门，不得在本任务临时修改未列入 **Files** 的业务文件。若发现分类语义残留，必须回到拥有该文件的前置任务补测试、修改并重新执行其验证命令。泛指任务目标的 `target_contact_id`、数学 target point 等不属于船舶分类，可保留；每个保留命中必须在进度文档审计清单中写明“文件:行号、命中词、保留理由”。

- [ ] **Step 4: 运行术语门禁和文档检查**

Run:

```bash
rg -n -i 'civilian|research|military|民船|民用船舶|科考船|舰船' \
  README.md docs src configs scripts tests \
  -g '!src/mission/vessel_compat.py' \
  -g '!src/vis/backend/replay_adapter.py' \
  -g '!tests/mission/test_vessel_compat.py' \
  -g '!tests/vis/test_replay_adapter.py' \
  -g '!docs/superpowers/specs/2026-09-16-dynamic-vessel-ais-information-design.md' \
  -g '!docs/superpowers/plans/2026-09-16-dynamic-vessel-ais-information-implementation-plan.md'
```

Expected: 仅剩经人工记录为通用非分类语义的命中；分类语义命中为 0。运行 `git diff --check` 必须无空白错误。

- [ ] **Step 5: 提交资料迁移**

```bash
git add README.md docs/2026-09-15-maritime-requirements.md docs/GOAL.md \
  docs/GOAL2.md docs/MIXED_MARITIME_VALIDATION.md docs/SYSTEM_PARAMS.md \
  docs/VALIDATION.md docs/implementation/mixed-maritime-progress.md \
  docs/superpowers/plans/2026-08-29-control-strategy-architecture-implementation-plan.md \
  docs/superpowers/plans/2026-09-14-mixed-maritime-llm-implementation-plan.md \
  docs/superpowers/plans/2026-09-15-maritime-requirements-alignment-implementation-plan.md \
  docs/superpowers/specs/2026-07-30-llm-dynamic-task-reallocation-design.md \
  docs/superpowers/specs/2026-08-29-control-strategy-architecture-design.md \
  docs/superpowers/specs/2026-09-14-mixed-maritime-llm-design.md \
  docs/superpowers/specs/2026-09-15-maritime-requirements-alignment-design.md \
  src/control/interface.md
git commit -m "docs: standardize type I and type II vessel terminology"
```

提交前必须从 staged diff 排除兼容层以外新增的旧词，并避免提交 outputs、构建产物或用户无关文件。

## Task 13: 删除临时兼容属性并执行全链路验收

**Files:**
- Modify: `src/mission/contracts.py`
- Modify: `src/env/ship.py`
- Modify: `src/env/simulation.py`
- Modify: `tests/mission/test_end_to_end.py`
- Modify: `tests/mission/test_maritime_acceptance.py`
- Modify: `tests/mission/test_dynamic_vessel_lifecycle.py`
- Modify: `tests/mission/test_information_loop.py`
- Modify: `src/vis/frontend/tests/mixed-maritime.spec.js`
- Modify: `docs/implementation/2026-09-16-dynamic-vessel-ais-progress.md`

**Interfaces:**
- Final runtime contract contains no `truth_identity`、`ais_mode`、旧分类 property 或双重 info state。
- Legacy support remains only in the two explicit input adapters。
- Acceptance helper contract: `create_runtime_type_ii` 通过 T3 队列创建并等待 applied；`detect_with_sar` 向正式 SAR observation 入口写入该船观测；`assign_probe` 提交并执行正式 probe intent；`acquire_eo_lock` 通过 EO sensor tick 建立 tracking；`red_call_stages` 从 scripted red gateway 的请求日志读取 stage；`every_installed_value_is_red_motion_parameters` 比较 `Ship.active_evasion` 与最新 `RedPlan` 五个允许字段；`executed_path_obeys_dynamics_and_boundary` 检查连续位置的速度/转向上限及 land/obstacle mask；`delete_runtime_vessel` 通过 T3 队列删除；`no_runtime_reference_remains` 枚举 T4 定义的全部运行时 registry，但明确排除 evidence/audit history。

- [ ] **Step 1: 写最终无旧接口与动态场景验收**

```python
def test_runtime_entities_expose_only_final_class_and_ais_contract(engine):
    ship = engine.ships[0]
    assert hasattr(ship, "vessel_class")
    assert hasattr(ship, "ais_enabled")
    assert not hasattr(ship, "truth_identity")
    assert not hasattr(ship, "ais_mode")

def test_dynamic_type_ii_closed_loop(engine_with_scripted_gateways):
    vessel = create_runtime_type_ii(engine_with_scripted_gateways)
    detect_with_sar(vessel)
    assign_probe(vessel)
    acquire_eo_lock(vessel)
    assert red_call_stages() == ["detected", "probing", "tracking"]
    assert every_installed_value_is_red_motion_parameters()
    assert executed_path_obeys_dynamics_and_boundary()
    delete_runtime_vessel(vessel)
    assert no_runtime_reference_remains(vessel.id)
```

端到端场景还必须切换 AIS off/on，断言 off 无新报文、旧 evidence 衰减、on 下一步新报文、信息版本连续、蓝方过期 selection 被拒绝、右侧数量与状态最终一致。

- [ ] **Step 2: 运行最终测试并确认临时兼容属性仍使测试失败**

Run: `python -m pytest tests/mission/test_end_to_end.py tests/mission/test_maritime_acceptance.py tests/mission/test_dynamic_vessel_lifecycle.py tests/mission/test_information_loop.py -q`

Expected: FAIL，仅因临时旧 property 仍存在或端到端断言尚未接通。

- [ ] **Step 3: 删除临时运行时兼容接口**

删除 T1 暂留的 `identity/truth_identity/ais_mode` properties、旧 event aliases、旧 metric properties 和旧配置 fallback。确认 `vessel_compat.py`、`replay_adapter.py` 不被任何新运行时输出调用。

- [ ] **Step 4: 运行完整后端、前端和静态门禁**

Run: `python -m pytest -q`

Expected: 全部 PASS，无 skipped 测试隐藏本需求。

Run: `npm --prefix src/vis/frontend run build`

Run: `npm --prefix src/vis/frontend run test:acceptance`

Expected: build 与全部 Playwright PASS；console/page error 为 0。

Run:

```bash
rg -n 'truth_identity|ais_mode|civilian_ratio|research_ratio|target_ais_on_probability' \
  src configs scripts tests \
  -g '!src/mission/vessel_compat.py' \
  -g '!src/vis/backend/replay_adapter.py' \
  -g '!tests/mission/test_vessel_compat.py' \
  -g '!tests/vis/test_replay_adapter.py'
```

Expected: 0 matches。

Run: `python scripts/evaluate_mixed_maritime.py --scenario mixed-ais --fixture --output-dir outputs/evaluations/type-ii-dynamic-fixture`

Expected: 命令成功；报告使用 I/II 指标名，明确 fixture 不代表真实模型准确率。

- [ ] **Step 5: 人工浏览器验收**

启动完整 FastAPI + WebSocket 仿真服务，不使用纯 Vite fixture 冒充后端。检查桌面和移动宽度：运行期放置/删除、II 类 AIS 开关、I 类禁用、动态计数、错误状态、回放只读、信息矩阵刷新均无重叠或跳动。保存 Playwright 截图到测试临时输出，不提交图片产物。

- [ ] **Step 6: 更新证据账本并提交**

进度文档记录最终 commit 前 HEAD、所有命令、通过数、真实模型调用是否发生、未验证项。没有 LongCat key 时明确写“真实在线模型准确率未验证”，不得用 scripted gateway 冒充。

```bash
git add src/mission/contracts.py src/env/ship.py src/env/simulation.py \
  tests/mission/test_end_to_end.py tests/mission/test_maritime_acceptance.py \
  tests/mission/test_dynamic_vessel_lifecycle.py tests/mission/test_information_loop.py \
  src/vis/frontend/tests/mixed-maritime.spec.js \
  docs/implementation/2026-09-16-dynamic-vessel-ais-progress.md
git commit -m "test: verify dynamic vessel AIS and information lifecycle"
```

## Final Review Gate

- [ ] 对照 D §1-§11 建立 requirement -> task -> test 映射，每条设计要求至少有一个自动化证据。
- [ ] `git status --short` 只包含用户原有文件或明确记录的产物；无 `dist/`、`outputs/`、缓存和截图进入提交。
- [ ] 审查所有新 API/frame/log 样例，只含 `type_i/type_ii` 与 `ais_enabled`。
- [ ] 审查蓝方 MissionSnapshot/Prompt，确认没有场景真值类别、surveillance stage 或物理 ship ID 泄露。
- [ ] 审查红方 Prompt，确认只有活动 II 类船舶、UAV 相对态势和参数约束，没有直接轨迹控制。
- [ ] 审查信息更新顺序，确认同一帧的 delta version 被调度快照读取，过期决策无法提交。
- [ ] 用户审核实施结果前，不自动 merge、push、部署或运行真实付费模型评估。
