# UAV Control Strategy Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有规则控制整合到 `src/control/heuristic`，建立 `bc`、`rl` 抽象基类与统一观测、动作、安全、执行和控制权运行时，并让仿真通过该运行时执行启发式覆盖/跟踪任务。

**Architecture:** `ControlCoordinator` 为每架 UAV 路由一次 `observe -> act -> safety -> execute`；`ControlOwnership` 显式区分系统、启发式任务和学习策略的控制权。启发式覆盖/跟踪采用 A* 转场并复用现有 CoveragePlanner、LGVF 和传感器模型；BC/RL 本轮只提供可继承基类，不提供具体模型实现。

**Tech Stack:** Python 3.10+、dataclasses、ABC、Enum、NumPy、PyYAML、pytest；复用现有 Dubins、CoveragePlanner、LGVFTracker 和障碍 mask。

## Global Constraints

- `src/control` 必须按 `heuristic`、`bc`、`rl`、`common` 四个目录组织。
- `heuristic/base.py`、`bc/base.py`、`rl/base.py` 必须分别声明对应实现基类。
- BC/RL 本轮不得增加具体控制器、模型依赖、训练代码或 Gymnasium 环境。
- 控制器每个仿真步只输出本机 `ControlCommand`，不得直接修改 `UAVEntity` 或 `StateManager`。
- BC/RL 从开始工作到工作里程耗尽保持控制权；普通任务事件不得回收控制权。
- 启发式控制权以任务为边界，任务切换必须通过事件流和 generation 原子替换。
- 工作里程耗尽定义为 `remaining_range <= validated_return_path + reserve + max_command_distance_next_tick`，不得用直线距离估算，也不得等待物理燃油为零。
- 安全层可以修正非法动作，但不得选择任务、目标或操作模式。
- 控制观测不得包含未发现舰船位置、真实军民属性或其他仿真隐藏真值。
- 启发式任务区域转场使用曲率受限 Hybrid A*，禁止穿障和切角；Dubins 只作为经碰撞校验的目标解析入场原语。
- 默认配置保持 `heuristic`，现有仿真无需 BC/RL 插件即可启动。
- 显式配置 BC/RL 且未注册具体子类时必须启动失败，禁止静默降级。
- 每个任务先写失败测试，再实现，再运行局部回归，最后单独提交。

---

## File Map

### New production files

- `configs/control.yaml`：控制模式、观测、安全和 A* 参数。
- `src/control/common/contracts.py`：稳定数据契约。
- `src/control/common/base.py`：公共控制器抽象。
- `src/control/common/observation.py`：无真值泄漏的观测构建。
- `src/control/common/safety.py`：命令校验和飞行安全约束。
- `src/control/common/executor.py`：统一动力学执行。
- `src/control/common/ownership.py`：lease 和 generation。
- `src/control/common/coordinator.py`：每步控制编排。
- `src/control/common/factory.py`：控制器注册和创建。
- `src/control/common/operation_registry.py`：动作意图与跟踪区/传感器绑定登记。
- `src/control/heuristic/base.py`：启发式任务控制基类和航路跟随。
- `src/control/heuristic/navigation.py`：A* + Dubins 路径。
- `src/control/heuristic/coverage.py`：覆盖控制器。
- `src/control/heuristic/tracking.py`：跟踪控制器。
- `src/control/heuristic/return_to_base.py`：返航规划器、返航控制器和系统等待控制器。
- `src/control/heuristic/task_flow.py`：启发式事件流转。
- `src/control/bc/base.py`：BC 抽象基类。
- `src/control/rl/base.py`：RL 抽象基类。
- 四个子目录的 `__init__.py`。

### Modified production files

- `src/control/__init__.py`：公开新 API。
- `src/control/waypoint.py`、`scan_pattern.py`、`track_orbit.py`、`return_path.py`：兼容转发。
- `src/schedule/config_loader.py`：加载 `ControlConfig`。
- `src/schedule/datatypes.py`：UAV 控制字段。
- `src/schedule/state_manager.py`：控制快照和可调度资格。
- `src/schedule/task_allocator.py`：只分配 heuristic UAV。
- `src/env/uav_entity.py`：公开统一动力学入口，移除新调用链中的任务决策。
- `src/env/simulation.py`：接入协调器、事件流和系统返航。
- `src/vis/backend/frame_builder.py`、`src/vis/backend/server.py`：输出控制诊断和配置。

### New tests

- `tests/control/test_contracts.py`
- `tests/control/test_base_classes.py`
- `tests/control/test_ownership.py`
- `tests/control/test_observation.py`
- `tests/control/test_safety.py`
- `tests/control/test_executor.py`
- `tests/control/test_coordinator.py`
- `tests/control/test_factory.py`
- `tests/control/test_operation_registry.py`
- `tests/control/test_simulation_ownership.py`
- `tests/control/heuristic/test_navigation.py`
- `tests/control/heuristic/test_coverage.py`
- `tests/control/heuristic/test_tracking.py`
- `tests/control/heuristic/test_task_flow.py`

---

### Task 1: Control configuration and package skeleton

**Files:**
- Create: `configs/control.yaml`
- Create: `src/control/common/__init__.py`
- Create: `src/control/heuristic/__init__.py`
- Create: `src/control/bc/__init__.py`
- Create: `src/control/rl/__init__.py`
- Modify: `src/schedule/config_loader.py`
- Modify: `tests/test_runtime_configuration.py`

**Interfaces:**
- Produces: `ControlConfig`, `ObservationControlConfig`, `SafetyControlConfig`, `HeuristicControlConfig`.
- Produces: `AppConfig.control: ControlConfig`.

- [ ] **Step 1: Write the failing configuration test**

Append to `tests/test_runtime_configuration.py`:

```python
def test_control_configuration_defaults_to_heuristic():
    config = ConfigLoader.load()

    assert config.control.default_mode == "heuristic"
    assert config.control.per_uav == {}
    assert config.control.observation.schema_version == "control-observation/v1"
    assert config.control.observation.local_window_cells == 11
    assert config.control.safety.reserve_range_cells == 4.0
    assert config.control.safety.max_invalid_commands == 3
    assert config.control.heuristic.astar_dynamic_replan_limit == 3
    assert config.control.heuristic.astar_xy_resolution_cells == 0.5
    assert config.control.heuristic.astar_heading_bins == 72
    assert config.control.heuristic.astar_candidate_limit == 32
    assert config.control.heuristic.astar_primitive_length_cells == 1.0
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python -m pytest tests/test_runtime_configuration.py::test_control_configuration_defaults_to_heuristic -q`

Expected: FAIL with `AttributeError: 'AppConfig' object has no attribute 'control'`.

- [ ] **Step 3: Add the exact configuration file**

Create `configs/control.yaml`:

```yaml
default_mode: heuristic
per_uav: {}

observation:
  schema_version: control-observation/v1
  local_window_cells: 11

safety:
  min_speed_fraction: 0.6
  max_speed_fraction: 1.2
  reserve_range_cells: 4.0
  max_invalid_commands: 3

heuristic:
  astar_dynamic_replan_limit: 3
  astar_xy_resolution_cells: 0.5
  astar_heading_bins: 72
  astar_candidate_limit: 32
  astar_primitive_length_cells: 1.0
  path_sample_step_cells: 0.2
```

- [ ] **Step 4: Add typed nested control configuration**

Add to `src/schedule/config_loader.py`:

```python
@dataclass(frozen=True)
class ObservationControlConfig:
    schema_version: str = "control-observation/v1"
    local_window_cells: int = 11


@dataclass(frozen=True)
class SafetyControlConfig:
    min_speed_fraction: float = 0.6
    max_speed_fraction: float = 1.2
    reserve_range_cells: float = 4.0
    max_invalid_commands: int = 3


@dataclass(frozen=True)
class HeuristicControlConfig:
    astar_dynamic_replan_limit: int = 3
    astar_xy_resolution_cells: float = 0.5
    astar_heading_bins: int = 72
    astar_candidate_limit: int = 32
    astar_primitive_length_cells: float = 1.0
    path_sample_step_cells: float = 0.2


@dataclass(frozen=True)
class ControlConfig:
    default_mode: str
    per_uav: dict[str, str]
    observation: ObservationControlConfig
    safety: SafetyControlConfig
    heuristic: HeuristicControlConfig
```

Add `control: ControlConfig` to `AppConfig`. In `ConfigLoader.load()`, read `control.yaml`, validate every configured mode against `{"heuristic", "bc", "rl"}`, require a positive odd `local_window_cells`, then construct the nested dataclasses:

```python
control_data = _read("control.yaml")
configured_modes = {
    control_data["default_mode"],
    *control_data.get("per_uav", {}).values(),
}
unknown_modes = configured_modes - {"heuristic", "bc", "rl"}
if unknown_modes:
    raise ValueError(f"unsupported control modes: {sorted(unknown_modes)}")
window = int(control_data["observation"]["local_window_cells"])
if window <= 0 or window % 2 == 0:
    raise ValueError("control observation local_window_cells must be a positive odd integer")
control = ControlConfig(
    default_mode=control_data["default_mode"],
    per_uav=dict(control_data.get("per_uav", {})),
    observation=ConfigLoader._dict_to_dataclass(
        control_data["observation"], ObservationControlConfig
    ),
    safety=ConfigLoader._dict_to_dataclass(
        control_data["safety"], SafetyControlConfig
    ),
    heuristic=ConfigLoader._dict_to_dataclass(
        control_data["heuristic"], HeuristicControlConfig
    ),
)
```

- [ ] **Step 5: Create package initializers**

Each new `__init__.py` contains only a module docstring in this task. Public re-exports are added after the referenced classes exist.

- [ ] **Step 6: Run configuration regression**

Run: `python -m pytest tests/test_runtime_configuration.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add configs/control.yaml src/control/common/__init__.py src/control/heuristic/__init__.py src/control/bc/__init__.py src/control/rl/__init__.py src/schedule/config_loader.py tests/test_runtime_configuration.py
git commit -m "feat(control): add control mode configuration"
```

---

### Task 2: Shared contracts and the three implementation base classes

**Files:**
- Create: `src/control/common/contracts.py`
- Create: `src/control/common/base.py`
- Create: `src/control/heuristic/base.py`
- Create: `src/control/bc/base.py`
- Create: `src/control/rl/base.py`
- Create: `tests/control/__init__.py`
- Create: `tests/control/test_contracts.py`
- Create: `tests/control/test_base_classes.py`

**Interfaces:**
- Produces: `ControlMode`, `ControlOwner`, `OperationMode`, `SensorMode`, `StopReason`.
- Produces: `Pose`, `ObservationSpec`, `ActionSpec`, `ActionMask`, `UAVObservation`, `ContactObservation`, `HazardObservation`, `BaseObservation`, `ControlObservation`, `ControlCommand`, `ControllerEventRequest`, `ControlDecision`, `RecoveryPlan`, `ControlTask`, `ControlEvent`, `ControllerContext`, `PolicySource`.
- Produces: `ControllerBase`, `LearningControllerBase`, `HeuristicControllerBase`, `BCControllerBase`, `RLControllerBase`.

- [ ] **Step 1: Write contract tests**

Create `tests/control/test_contracts.py`:

```python
import numpy as np

from src.control.common.contracts import (
    ActionMask,
    ControlCommand,
    ControlMode,
    OperationMode,
    SensorMode,
)


def test_control_command_uses_physical_step_units():
    command = ControlCommand(
        turn_rate_rad_min=0.2,
        speed_cells_min=0.25,
        sensor_mode=SensorMode.SAR,
        operation_mode=OperationMode.COVERAGE,
    )

    assert command.turn_rate_rad_min == 0.2
    assert command.speed_cells_min == 0.25
    assert command.target_contact_id is None


def test_action_mask_is_immutable_and_mode_specific():
    mask = ActionMask(
        allowed_sensor_modes=(SensorMode.OFF, SensorMode.SAR),
        allowed_operation_modes=(OperationMode.TRANSIT, OperationMode.COVERAGE),
        target_contact_ids=(),
    )

    assert SensorMode.EO not in mask.allowed_sensor_modes
    assert ControlMode("heuristic") is ControlMode.HEURISTIC
```

- [ ] **Step 2: Write base-class tests**

Create `tests/control/test_base_classes.py` with minimal concrete test doubles for BC and RL. Assert that the three base classes cannot be instantiated directly, BC `act()` executes encode/predict/decode exactly once, and RL `reset()` initializes policy state by calling `initial_policy_state()`.

Core assertion:

```python
def test_bc_template_method_returns_decoded_command(bc_controller, observation):
    result = bc_controller.act(observation)

    assert result.command.operation_mode is OperationMode.COVERAGE
    assert bc_controller.calls == ["encode", "predict", "decode"]
```

- [ ] **Step 3: Run tests and verify import failures**

Run: `python -m pytest tests/control/test_contracts.py tests/control/test_base_classes.py -q`

Expected: collection ERROR because the new modules do not exist.

- [ ] **Step 4: Implement complete shared contracts**

Implement the enums and frozen dataclasses from the design document. Add these exact supporting types so later tasks use one spelling:

```python
@dataclass(frozen=True)
class ObservationSpec:
    schema_version: str
    local_window_cells: int
    array_dtype: str = "float32"


@dataclass(frozen=True)
class ActionSpec:
    min_turn_rate_rad_min: float
    max_turn_rate_rad_min: float
    min_speed_cells_min: float
    max_speed_cells_min: float


@dataclass(frozen=True)
class ActionMask:
    allowed_sensor_modes: tuple[SensorMode, ...]
    allowed_operation_modes: tuple[OperationMode, ...]
    target_contact_ids: tuple[str, ...]


Pose = tuple[float, float, float]


@dataclass(frozen=True)
class BaseObservation:
    base_id: str
    position: tuple[float, float]
    capacity: int
    reserved_load: int


@dataclass(frozen=True)
class RecoveryPlan:
    base_id: str
    base_position: tuple[float, float]
    reservation_id: str
    path: tuple[Pose, ...]
    path_length_cells: float
    reserve_cells: float
    planning_map_version: int


@dataclass(frozen=True)
class ControllerEventRequest:
    event_type: str
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ControlDecision:
    command: ControlCommand
    events: tuple[ControllerEventRequest, ...] = ()


@dataclass(frozen=True)
class ControllerContext:
    uav_id: str
    dt_min: float
    observation_spec: ObservationSpec
    action_spec: ActionSpec
    episode_id: str
    task: ControlTask | None = None


@dataclass(frozen=True)
class PolicySource:
    uri: str
    metadata: Mapping[str, object] = field(default_factory=dict)
```

Import `BBox` from `src.schedule.datatypes`; use the local `Pose` alias above rather than importing `src.env.dubins.Pose`, preventing a `control -> env -> control` cycle. Add `schema_version: str = "control-command/v1"` as the final `ControlCommand` field.

Use `MappingProxyType(dict(payload))` in `ControlEvent.__post_init__` so callers cannot mutate event payload after delivery. Mark NumPy arrays in `ControlObservation.__post_init__` with `array.setflags(write=False)`.

- [ ] **Step 5: Implement the common and specialized bases**

Implement the signatures in the design document. `ControllerBase.reset()` is concrete and stores the immutable context. `BCControllerBase.act()` is concrete:

```python
def act(self, observation: ControlObservation) -> ControlDecision:
    encoded = self.encode_observation(observation)
    output = self.predict_action(encoded)
    return ControlDecision(command=self.decode_action(output))
```

`RLControllerBase.reset()` calls the concrete `super().reset(context)`, initializes `_policy_state = initial_policy_state()`, defaults `_deterministic = True`, and calls `reset_episode(context.episode_id)` once when the coordinator starts a sortie. Its concrete `act()` passes `_deterministic` and `_policy_state` to `predict_action()`, stores the returned next state, and wraps the command in `ControlDecision`. `set_evaluation_mode(enabled)` sets `_deterministic = enabled`. Do not import Gymnasium or a model framework.

Add this property to `HeuristicControllerBase` so task flow and diagnostics do not infer the operation from class names:

```python
@property
@abstractmethod
def operation_mode(self) -> OperationMode:
    raise NotImplementedError
```

- [ ] **Step 6: Run contract tests**

Run: `python -m pytest tests/control/test_contracts.py tests/control/test_base_classes.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/control/common/contracts.py src/control/common/base.py src/control/heuristic/base.py src/control/bc/base.py src/control/rl/base.py tests/control
git commit -m "feat(control): define shared and strategy base contracts"
```

---

### Task 3: Explicit control ownership and stale-command rejection

**Files:**
- Create: `src/control/common/ownership.py`
- Create: `tests/control/test_ownership.py`

**Interfaces:**
- Produces: `ControlLease`.
- Produces: `ControlOwnership.acquire()`, `replace()`, `release_to_system()`, `current()`, `accepts()`.

- [ ] **Step 1: Write ownership tests**

Create `tests/control/test_ownership.py`:

```python
from src.control.common.contracts import ControlOwner
from src.control.common.ownership import ControlOwnership


def test_replacing_heuristic_task_invalidates_old_lease():
    ownership = ControlOwnership(["UAV-1"])
    first = ownership.acquire("UAV-1", ControlOwner.HEURISTIC, "coverage:S1", 1.0)
    second = ownership.replace(first, ControlOwner.HEURISTIC, "tracking:G1", 2.0)

    assert second.generation == first.generation + 1
    assert not ownership.accepts(first)
    assert ownership.accepts(second)


def test_learning_task_events_do_not_replace_sortie_lease():
    ownership = ControlOwnership(["UAV-1"])
    lease = ownership.acquire("UAV-1", ControlOwner.LEARNING, "rl:UAV-1", 1.0)

    assert ownership.current("UAV-1") == lease
    assert ownership.accepts(lease)


def test_release_requires_current_generation():
    ownership = ControlOwnership(["UAV-1"])
    lease = ownership.acquire("UAV-1", ControlOwner.HEURISTIC, "coverage:S1", 1.0)
    ownership.release_to_system(lease, 10.0)

    assert ownership.current("UAV-1").owner is ControlOwner.SYSTEM
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/control/test_ownership.py -q`

Expected: collection ERROR for missing `ownership.py`.

- [ ] **Step 3: Implement ownership atomically**

Use this lease shape:

```python
@dataclass(frozen=True)
class ControlLease:
    uav_id: str
    owner: ControlOwner
    controller_id: str
    generation: int
    acquired_at_min: float
```

Initialize every UAV with `ControlOwner.SYSTEM`, controller ID `system`, generation `0`. `acquire()` only accepts a current SYSTEM lease; `replace()` requires the supplied lease to be current; `release_to_system()` increments generation. Raise `ControlOwnershipError` with UAV ID and both generations on mismatches.

- [ ] **Step 4: Run ownership tests**

Run: `python -m pytest tests/control/test_ownership.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/control/common/ownership.py tests/control/test_ownership.py
git commit -m "feat(control): add explicit control ownership leases"
```

---

### Task 4: Observation provider with truth isolation

**Files:**
- Create: `src/control/common/observation.py`
- Modify: `src/schedule/state_manager.py`
- Create: `tests/control/test_observation.py`

**Interfaces:**
- Consumes: `UAVEntity`, `StateManager`, ordered `ControlEvent` snapshots.
- Produces: `ObservationProvider.build(entity, state_manager, *, events, bases, control_mode, control_owner, operation_mode, safety_intervened, current_time, dt_min) -> ControlObservation`.

- [ ] **Step 1: Write observation tests**

Create tests that build an engine with a hidden ship, set hidden truth fields to sentinel values, and build an observation without passing `engine.ships`:

```python
def test_observation_excludes_undetected_ship_truth():
    engine = SimulationEngine(ConfigLoader.load(), seed=17)
    hidden = engine.ships[0]
    hidden._col = 27.12345
    hidden._row = 26.54321
    hidden.actual_military = True

    observation = make_provider(engine).build(
        engine.uavs[0],
        engine.allocator.sm,
        events=(),
        bases=make_base_observations(engine.bases),
        control_mode=ControlMode.HEURISTIC,
        control_owner=ControlOwner.SYSTEM,
        operation_mode=OperationMode.IDLE,
        safety_intervened=False,
        current_time=engine.clock.time,
        dt_min=engine.clock.dt_min,
    )

    assert observation.contacts == ()
    assert "27.12345" not in repr(observation)
    assert "actual_military" not in repr(observation)
```

Also assert:

```python
assert observation.local_info.shape == (11, 11)
assert observation.local_info.dtype == np.float32
assert not observation.local_info.flags.writeable
assert [event.sequence for event in observation.events] == [1, 2]
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/control/test_observation.py -q`

Expected: collection ERROR for missing `ObservationProvider`.

- [ ] **Step 3: Implement local-window extraction**

Pad outside-map cells deterministically:

- `local_info`: `0.0`
- `local_value`: `0.0`
- `obstacle_mask`: `True`
- `searchable_mask`: `False`

Center the odd window on rounded UAV grid coordinates. Convert numeric arrays to `np.float32`, masks to `np.bool_`, and make all arrays read-only.

Copy the complete `state_manager.obstacle_mask` into `planning_obstacle_mask`, mark it read-only, and publish `state_manager.obstacle_version`. Increment `obstacle_version` in `set_environment_obstacles()` only when the normalized mask changes.

- [ ] **Step 4: Build contacts only from reports**

Iterate `state_manager.get_target_reports()`, calculate age from the required `current_time`, and expose estimated position/velocity. Convert published islands and thunderstorms from `state_manager.obstacles` into immutable `HazardObservation` snapshots containing only ID, type, geometry, motion and intensity. Accept already immutable `BaseObservation` values from the coordinator. Do not accept `ships` as a constructor or method argument. Build `shared_uavs` from `state_manager.get_all_uavs()` and sort contacts, hazards, bases and UAVs by ID for deterministic output.

Build `ActionMask` deterministically from owner, mode and reports. HEURISTIC/LEARNING work leases allow TRANSIT and COVERAGE, plus TRACK/EO only when a contact exists. A SYSTEM return lease allows RETURN and HOLDING only. OFF is always allowed; SAR is allowed as a work request; `target_contact_ids` is exactly the sorted contact ID tuple. The mask reports authority/resource availability, not the controller's chosen task.

- [ ] **Step 5: Run observation and state tests**

Run: `python -m pytest tests/control/test_observation.py tests/schedule/test_state_manager.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/control/common/observation.py src/schedule/state_manager.py tests/control/test_observation.py
git commit -m "feat(control): provide truth-isolated controller observations"
```

---

### Task 5: Safety envelope and unified dynamics executor

**Files:**
- Create: `src/control/common/safety.py`
- Create: `src/control/common/executor.py`
- Modify: `src/env/uav_entity.py`
- Create: `tests/control/test_safety.py`
- Create: `tests/control/test_executor.py`

**Interfaces:**
- Produces: `SafetyIntervention`, `SafetyResult`, `SafetyEnvelope.apply()`.
- Produces: `ExecutionResult`, `UAVDynamicsExecutor.execute()`.
- Produces: `UAVEntity.apply_motion()` as the only new low-level pose mutation API.

- [ ] **Step 1: Write safety tests**

Cover non-finite actions, turn/speed clipping, predicted boundary collision, obstacle collision, and SAR outside a stable coverage leg. A representative assertion:

```python
def test_safety_preserves_task_intent_while_avoiding_blocked_step(setup):
    envelope, observation = setup
    requested = ControlCommand(
        turn_rate_rad_min=10.0,
        speed_cells_min=10.0,
        sensor_mode=SensorMode.EO,
        operation_mode=OperationMode.TRACK,
        target_contact_id="G1",
    )

    result = envelope.apply(requested, observation, dt_min=1.0)

    assert result.applied_command.operation_mode is OperationMode.TRACK
    assert result.applied_command.target_contact_id == "G1"
    assert result.interventions
```

- [ ] **Step 2: Write executor tests**

```python
def test_executor_integrates_midpoint_motion_and_consumes_actual_range(uav):
    executor = UAVDynamicsExecutor()
    command = ControlCommand(
        turn_rate_rad_min=0.2,
        speed_cells_min=0.25,
        sensor_mode=SensorMode.OFF,
        operation_mode=OperationMode.TRANSIT,
    )

    before = uav.remaining_range_cells
    result = executor.execute(uav, command, dt_min=1.0)

    assert result.distance_cells == pytest.approx(0.25)
    assert uav.remaining_range_cells == pytest.approx(before - 0.25)
    assert uav.heading_rad == pytest.approx(0.2)
```

- [ ] **Step 3: Run tests and verify failure**

Run: `python -m pytest tests/control/test_safety.py tests/control/test_executor.py -q`

Expected: collection ERROR for missing safety/executor modules.

- [ ] **Step 4: Implement SafetyEnvelope**

Require `command.schema_version == "control-command/v1"`, then validate `math.isfinite()` for both continuous fields. Clamp to `ActionSpec`. Predict motion using midpoint integration. If requested motion is blocked, evaluate legal candidates in deterministic order `(requested turn, +max turn, -max turn, 0)` at the minimum legal speed and choose the first collision-free candidate. If none is safe, raise `UnsafeControlState` rather than silently entering a blocked cell.

Reject an operation mode or target contact that is absent from `action_mask`; do not substitute another operation or target. Force SAR to OFF when the requested operation is not COVERAGE or the applied turn rate exceeds the SAR heading-stability tolerance; record `sensor_mode_masked` without changing `operation_mode` or `target_contact_id`.

- [ ] **Step 5: Add the entity motion primitive and executor**

Add to `UAVEntity`:

```python
def apply_motion(
    self,
    turn_rate_rad_min: float,
    speed_cells_min: float,
    dt_min: float,
) -> float:
    mid_heading = self.heading_rad + turn_rate_rad_min * dt_min / 2.0
    distance = speed_cells_min * dt_min
    self._col += distance * math.cos(mid_heading)
    self._row += distance * math.sin(mid_heading)
    self.heading_rad = _wrap_pi(self.heading_rad + turn_rate_rad_min * dt_min)
    self._distance_this_step = distance
    self.fuel_remaining_pct = max(
        0.0,
        self.fuel_remaining_pct - distance / self.total_range_cells,
    )
    return distance
```

`UAVDynamicsExecutor.execute()` calls only this method for pose movement, maps `OperationMode` to legacy `status`, updates sensor mode, trail and last requested/applied commands, and returns an immutable `ExecutionResult`.

- [ ] **Step 6: Run local regressions**

Run: `python -m pytest tests/control/test_safety.py tests/control/test_executor.py tests/env/test_dubins.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/control/common/safety.py src/control/common/executor.py src/env/uav_entity.py tests/control/test_safety.py tests/control/test_executor.py
git commit -m "feat(control): add safe unified UAV command execution"
```

---

### Task 6: Curvature-constrained Hybrid A* navigation

**Files:**
- Create: `src/control/heuristic/navigation.py`
- Create: `tests/control/heuristic/__init__.py`
- Create: `tests/control/heuristic/test_navigation.py`

**Interfaces:**
- Produces: `PathNotFoundError`.
- Produces: `AStarNavigator.plan_to_region(start_pose, bbox, obstacle_mask, r_min, planning_map_version) -> list[Pose]`.
- Produces: `AStarNavigator.plan_to_standoff(start_pose, target, radius, obstacle_mask, r_min, planning_map_version) -> list[Pose]`.

- [ ] **Step 1: Write A* behavior tests**

Create tests for direct path, wall detour, unreachable goal, diagonal corner cutting, region boundary selection, and final Dubins safety:

```python
def test_astar_does_not_cut_a_blocked_diagonal_corner():
    mask = np.ones((4, 4), dtype=bool)
    mask[1, 1] = False
    mask[2, 2] = False
    mask[2, 1] = True
    mask[1, 2] = True
    navigator = AStarNavigator(sample_step=0.2)

    with pytest.raises(PathNotFoundError):
        navigator.plan_grid((1.0, 1.0, 0.0), {(2.0, 2.0)}, mask, r_min=1.0)


def test_region_path_ends_on_region_boundary_and_is_safe():
    mask = np.zeros((30, 30), dtype=bool)
    path = AStarNavigator().plan_to_region(
        (2.0, 2.0, 0.0), BBox(10, 10, 15, 15), mask, r_min=1.0
    )

    assert _on_bbox_boundary(path[-1][:2], BBox(10, 10, 15, 15))
    assert ObstacleAvoider.is_path_safe(path, mask)


def test_large_turn_radius_can_still_turn_in_open_space():
    mask = np.zeros((80, 80), dtype=bool)
    path = AStarNavigator(heading_bins=72).plan_grid(
        (10.0, 10.0, 0.0), {(10.0, 40.0)}, mask, r_min=8.0
    )

    assert path[-1][1] == pytest.approx(40.0, abs=1.0)
    assert _all_samples_respect_curvature(path, r_min=8.0)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/control/heuristic/test_navigation.py -q`

Expected: collection ERROR for missing `AStarNavigator`.

- [ ] **Step 3: Implement deterministic curvature-constrained Hybrid A***

Each search node stores continuous `(x, y, heading)` and a closed-set key `(round(x / xy_resolution), round(y / xy_resolution), heading_bin)`. Use stable `heapq` entries `(f_score, g_score, key, insertion_sequence)`. Expand curvature primitives `{-1/r_min, 0, +1/r_min}` by exact line/arc integration. Primitive length is `max(astar_primitive_length_cells, 2*pi*r_min/astar_heading_bins)`, so a turn always advances by at least one heading bin instead of becoming impossible for large `r_min`. Sample each primitive at `path_sample_step_cells`; reject it on boundary/obstacle collision or corner cutting. Reconstruct the sampled continuous poses from `came_from`.

The public grid method has this exact signature:

```python
def plan_grid(
    self,
    start: Pose,
    goals: set[tuple[float, float]],
    obstacle_mask: np.ndarray,
    r_min: float,
    planning_map_version: int = 0,
) -> list[Pose]:
```

Use existing half-open `BBox` semantics. Region goal samples lie on the inner perimeter of `[col_start, col_end - 1] x [row_start, row_end - 1]`; `GridCoord(col,row)` maps to pose `(float(col), float(row), heading)`.

- [ ] **Step 4: Add region/standoff goals and Dubins analytic expansion**

Enumerate unblocked bbox boundary samples or standoff annulus samples in `(estimated total cost, col, row)` order. Search motion primitives already form a continuous curvature-safe path and must not be replaced by line-of-sight simplification.

When a node is close enough to a candidate, attempt a Dubins analytic connection from that exact node pose to the goal pose and validate every sample. If unsafe, record the exact `(node closed-set key, goal pose)` attempt and continue expanding the open set; do not map a multi-segment Dubins failure back to an arbitrary search edge. Try at most `astar_candidate_limit` analytic connections. If the open set is exhausted without a safe primitive/analytic path, raise `PathNotFoundError` with the start, goal summary, planning-map version and attempt count.

- [ ] **Step 5: Run navigation and obstacle tests**

Run: `python -m pytest tests/control/heuristic/test_navigation.py tests/utils/test_obstacle_avoider.py tests/env/test_dubins.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/control/heuristic/navigation.py tests/control/heuristic
git commit -m "feat(control): add hybrid A-star fixed-wing navigation"
```

---

### Task 7: Heuristic coverage controller and legacy scan integration

**Files:**
- Create: `src/control/heuristic/coverage.py`
- Modify: `src/control/heuristic/base.py`
- Modify: `src/control/waypoint.py`
- Modify: `src/control/scan_pattern.py`
- Create: `tests/control/heuristic/test_coverage.py`

**Interfaces:**
- Produces: `RouteFollower.next_command()` in the heuristic base module.
- Produces: `CoveragePhase` and `CoverageController`.
- Keeps: `navigate_to_region()` and `generate_scan_waypoints()` as deprecated compatibility functions.

- [ ] **Step 1: Write coverage-controller tests**

Test phase order, A* use, SAR gating, task completion, and dynamic route invalidation. Use a navigator spy and the real `CoveragePlanner`:

```python
def test_coverage_uses_astar_before_enabling_sar(controller, observation):
    controller.start_task(
        ControlTask("S1", OperationMode.COVERAGE, region_bbox=BBox(10, 10, 15, 15)),
        observation,
    )

    decision = controller.act(observation)

    assert controller.navigator.plan_calls == 1
    assert controller.phase is CoveragePhase.TRANSIT_ASTAR
    assert decision.command.sensor_mode is SensorMode.OFF


def test_coverage_enables_sar_only_on_stable_scan_leg(controller, scan_observation):
    decision = controller.act(scan_observation)

    assert controller.phase is CoveragePhase.SCANNING
    assert decision.command.sensor_mode is SensorMode.SAR
    assert decision.command.operation_mode is OperationMode.COVERAGE
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/control/heuristic/test_coverage.py -q`

Expected: collection ERROR for missing `CoverageController`.

- [ ] **Step 3: Implement route following in the base**

`RouteFollower` stores immutable poses plus an index. It calculates desired heading from the next pose, wraps heading error to `[-pi, pi]`, divides by `dt_min`, clamps to `ActionSpec`, and advances the index only when distance is within `max(speed * dt, 0.05)`. It never writes entity state.

Do not make `AStarNavigator` a controller: navigation is a phase within coverage, tracking and system return, not an independently assigned task. Every class that emits heuristic commands must inherit `HeuristicControllerBase`; `AStarNavigator` only returns immutable paths.

- [ ] **Step 4: Implement CoverageController**

Build the coverage scan swaths with the existing `CoveragePlanner`. Plan A* only to the first scan entry, then concatenate the existing scan legs and Dubins connectors. Set phases using route indices. Request SAR only strictly inside a scan range with heading error at or below the configured tolerance; `ActionMask` remains the observation provider's environment-resource mask. `is_complete()` returns true only after the final scan pose is consumed. On the first completed tick, return exactly one `ControllerEventRequest("search_complete", {"task_id": task.task_id})` in `ControlDecision.events`.

- [ ] **Step 5: Replace old functions with compatibility wrappers**

`src/control/waypoint.py:navigate_to_region()` calls `AStarNavigator.plan_to_region()` against an all-free 30x30 compatibility mask and converts output poses to `GridCoord` for the old return type. `src/control/scan_pattern.py:generate_scan_waypoints()` delegates to the scan-endpoint helper used by `CoverageController`, preserving the old coarse waypoint return type. Both emit `DeprecationWarning` with `stacklevel=2`.

- [ ] **Step 6: Run coverage regressions**

Run: `python -m pytest tests/control/heuristic/test_coverage.py tests/utils/test_coverage_planner.py tests/env/test_simulation_integration.py::test_sar_is_off_during_dubins_turns -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/control/heuristic/base.py src/control/heuristic/coverage.py src/control/waypoint.py src/control/scan_pattern.py tests/control/heuristic/test_coverage.py
git commit -m "feat(control): integrate heuristic coverage controller"
```

---

### Task 8: Heuristic tracking and system return controllers

**Files:**
- Create: `src/control/heuristic/tracking.py`
- Create: `src/control/heuristic/return_to_base.py`
- Modify: `src/control/track_orbit.py`
- Modify: `src/control/return_path.py`
- Create: `tests/control/heuristic/test_tracking.py`

**Interfaces:**
- Produces: `TrackingPhase`, `TrackingController`.
- Produces: `RecoveryCandidate`, `RecoveryPlanner`, `ReturnToBaseController`, `SystemHoldingController`.
- Keeps: old orbit/return functions as deprecated wrappers.

- [ ] **Step 1: Write tracking tests**

Test A* approach based on `ContactObservation`, LGVF orbit command, contact update, storm safety delegation, and controller-generated route failures:

```python
def test_tracking_uses_reported_contact_not_environment_truth(controller, observation):
    controller.start_task(
        ControlTask("T1", OperationMode.TRACK, target_contact_id="contact:G1"),
        observation,
    )

    controller.act(observation)

    assert controller.navigator.last_target == (12.0, 13.0)


def test_tracking_reports_internal_route_failure(controller, blocked_observation):
    decision = controller.act(blocked_observation)

    assert [event.event_type for event in decision.events] == ["task_failed"]
```

- [ ] **Step 2: Write return tests**

Assert that `RecoveryPlanner` evaluates every non-full base with Hybrid A* plus checked Dubins analytic expansion, rejects candidates whose actual safe path length plus reserve exceeds remaining range, and sorts feasible candidates by `(path_length_cells, base_id)`. Assert that `ReturnToBaseController.start_task()` rejects a RETURN task without `RecoveryPlan`, follows only `recovery_plan.path`, emits RETURN/OFF commands, and releases an unused reservation from `stop_task()`. Change the planning map version and block the remaining path; assert that the controller retains the same base reservation, replans to that base, rechecks remaining range, and raises `NoSafeRecoveryPath` without executing the old path when replanning fails. Assert that `SystemHoldingController` emits HOLDING/OFF and continues producing a safety-checked fixed-wing waiting orbit until assignment.

- [ ] **Step 3: Run tests and verify failure**

Run: `python -m pytest tests/control/heuristic/test_tracking.py -q`

Expected: collection ERROR for missing tracking and return modules.

- [ ] **Step 4: Implement TrackingController**

Resolve `target_contact_id` only against `observation.contacts`. Use `AStarNavigator.plan_to_standoff()` until the UAV is within EO range, `LGVFTracker.plan_entry()` for orbit entry, then `LGVFTracker.compute_guidance()` for per-step turn rate and speed. Convert `observation.hazards` to the immutable geometry protocol consumed by storm avoidance; never pass environment obstacle objects into the controller. Contact updates replace the internal estimated center only when their timestamp is newer.

Do not accept a ship object or ground-truth callback in the constructor.

- [ ] **Step 5: Implement ReturnToBaseController**

Define the internal immutable candidate exactly:

```python
@dataclass(frozen=True)
class RecoveryCandidate:
    base: BaseObservation
    path: tuple[Pose, ...]
    path_length_cells: float
    reserve_cells: float
    planning_map_version: int
```

Add `RecoveryPlanner.evaluate(start_pose, remaining_range_cells, bases, planning_obstacle_mask, planning_map_version, r_min, reserve_cells) -> tuple[RecoveryCandidate, ...]`. For every base with `reserved_load < capacity`, call Hybrid A* plus checked Dubins analytic expansion first, compute the actual path length, retain only `path_length + reserve_cells <= remaining_range_cells`, and sort by `(path_length_cells, base.base_id)`. It must not reserve capacity itself.

`ReturnToBaseController.start_task()` requires `task.task_type is RETURN` and a non-null `task.recovery_plan`; it copies the validated path into `RouteFollower` and records `planning_map_version`. In `act()`, a version change triggers validation of the unflown suffix. If blocked, replan to the same reserved base against the new mask, retain the reservation, and accept the route only when `new_path_length + reserve_cells <= remaining_range_cells`; otherwise raise `NoSafeRecoveryPath`. Never switch to an unreserved base or retain the blocked route. `is_complete()` checks arrival at `base_position`. `stop_task()` invokes an injected reservation-release callback only when arrival did not consume the reservation. `SystemHoldingController` builds a local fixed-wing waiting orbit from the current observation and emits HOLDING/OFF. Both remain `HeuristicControllerBase` implementations, but their lease owner is SYSTEM.

- [ ] **Step 6: Add compatibility wrappers**

Keep the existing public functions in `track_orbit.py` and `return_path.py`, delegate them to the corresponding new helpers, and emit `DeprecationWarning`. Preserve their current argument and return types during the compatibility period.

- [ ] **Step 7: Run tracking regressions**

Run: `python -m pytest tests/control/heuristic/test_tracking.py tests/utils/test_track_orbit.py tests/env/test_storm_avoidance.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/control/heuristic/tracking.py src/control/heuristic/return_to_base.py src/control/track_orbit.py src/control/return_path.py tests/control/heuristic/test_tracking.py
git commit -m "feat(control): integrate heuristic tracking and return control"
```

---

### Task 9: Heuristic task flow, factory, and scheduler eligibility

**Files:**
- Create: `src/control/heuristic/task_flow.py`
- Create: `src/control/common/factory.py`
- Create: `src/control/common/operation_registry.py`
- Modify: `src/schedule/datatypes.py`
- Modify: `src/schedule/state_manager.py`
- Modify: `src/schedule/task_allocator.py`
- Create: `tests/control/heuristic/test_task_flow.py`
- Create: `tests/control/test_factory.py`
- Create: `tests/control/test_operation_registry.py`
- Modify: `tests/schedule/test_state_manager.py`
- Modify: `tests/schedule/test_task_allocator.py`

**Interfaces:**
- Produces: `TaskTransition`, `HeuristicTaskFlow.handle()`.
- Produces: `ControlFactory.register()` and `create_learning()`.
- Produces: `OperationRegistry.reconcile()`.
- Extends: `UAVState` with control snapshot fields.

- [ ] **Step 1: Write atomic task-flow tests**

Create a coverage lease, deliver `target_found`, and assert one replacement lease plus a tracking controller. Deliver `target_lost` and assert return to the saved coverage task. Deliver `search_complete` without pending work and assert release to SYSTEM/HOLDING with a system holding controller. Deliver task events to a LEARNING lease and assert that task flow does not consume them or replace the lease.

```python
assert transition.previous_lease.generation + 1 == transition.current_lease.generation
assert transition.current_lease.owner is ControlOwner.HEURISTIC
assert transition.controller.operation_mode is OperationMode.TRACK
```

- [ ] **Step 2: Write factory and scheduler tests**

```python
def test_unregistered_learning_mode_fails_fast(config):
    factory = ControlFactory(config.control)

    with pytest.raises(ControlFactoryError, match="UAV-1.*bc.*not registered"):
        factory.create_learning("UAV-1", ControlMode.BC)


def test_available_uavs_excludes_learning_owned_airframe(sm):
    sm.update_uav_control("UAV-1", "bc", "learning", "coverage", 1, False)

    assert "UAV-1" not in {uav.id for uav in sm.get_available_uavs()}
```

- [ ] **Step 3: Run tests and verify failure**

Run: `python -m pytest tests/control/heuristic/test_task_flow.py tests/control/test_factory.py tests/schedule/test_state_manager.py tests/schedule/test_task_allocator.py -q`

Expected: FAIL because task flow, factory and state fields are absent.

- [ ] **Step 4: Implement HeuristicTaskFlow**

Map only these events:

```python
EVENT_TRANSITIONS = {
    "target_found": OperationMode.TRACK,
    "target_lost": OperationMode.COVERAGE,
    "civilian_released": OperationMode.COVERAGE,
    "target_departed": OperationMode.COVERAGE,
    "search_complete": OperationMode.HOLDING,
    "task_failed": OperationMode.HOLDING,
}
```

For heuristic leases, construct and validate the replacement controller/task first. Commit the lease replacement, controller registry and pending task together under the coordinator lock; then call the old controller's idempotent `stop_task()` for resource cleanup, never for another command. For learning leases, return `TaskTransition.unchanged()` for all normal task events. `work_range_exhausted` is a lifecycle event handled outside this task flow: the engine first constructs and reserves a valid recovery plan, then atomically revokes either HEURISTIC or LEARNING to SYSTEM.

Define the result exactly:

```python
@dataclass(frozen=True)
class TaskTransition:
    consumed: bool
    previous_lease: ControlLease
    current_lease: ControlLease
    controller: ControllerBase
    task: ControlTask | None
    request_assignment: bool = False
```

A consumed transition event is not delivered again through `ControlObservation.events`. `handle()` only constructs and registers the replacement plus its pending task; it cannot call `start_task()` because the new-lease observation does not exist yet. If there is no successor work task, `current_lease` is SYSTEM, `controller` is `SystemHoldingController`, `task` is a HOLDING task, and `request_assignment` is true.

- [ ] **Step 5: Implement fail-fast factory**

Factory keeps providers keyed by `ControlMode`. Heuristic task controllers are built-in. BC/RL providers are absent by default and can only be registered explicitly. Reject duplicate registration and provider results whose `control_mode` does not match the requested mode.

- [ ] **Step 6: Implement operation registration for all applied commands**

Create `OperationRegistry.reconcile(uav_id, previous_command, applied_command, observation)`. Validate that TRACK references an ID in `observation.action_mask.target_contact_ids`; resolve the matching `ContactObservation.group_id`; create or bind the corresponding track region on entry; update the binding while TRACK continues; and release this UAV's binding when leaving TRACK. It may update `StateManager` occupancy and sensor binding only. It must not generate flight commands, select a successor task, or read ship objects.

Add tests using minimal concrete BC/RL test controllers registered through `ControlFactory`: entering TRACK creates one track region, continuing does not duplicate it, leaving TRACK releases it, and an unknown contact ID raises `InvalidOperationIntent` without changing world state.

- [ ] **Step 7: Extend scheduling state**

Add defaults to `UAVState`:

```python
control_mode: str = "heuristic"
control_owner: str = "system"
operation_mode: str = "idle"
controller_generation: int = 0
safety_intervened: bool = False
```

Add `StateManager.update_uav_control()` with all five control fields required. Change `get_available_uavs()` to return only heuristic UAVs owned by SYSTEM whose operation mode is IDLE or HOLDING. Keep defaults on `UAVState` itself for construction compatibility with existing tests.

- [ ] **Step 8: Make TaskAllocator use eligibility explicitly**

Both light and heavy pairing paths must call the filtered `get_available_uavs()`. Add an assertion-level test that an idle BC/RL state is never included in Hungarian input even when a region is unassigned.

- [ ] **Step 9: Run task flow, registry, and scheduler tests**

Run: `python -m pytest tests/control/heuristic/test_task_flow.py tests/control/test_factory.py tests/control/test_operation_registry.py tests/schedule/test_state_manager.py tests/schedule/test_task_allocator.py -q`

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/control/heuristic/task_flow.py src/control/common/factory.py src/control/common/operation_registry.py src/schedule/datatypes.py src/schedule/state_manager.py src/schedule/task_allocator.py tests/control/heuristic/test_task_flow.py tests/control/test_factory.py tests/control/test_operation_registry.py tests/schedule/test_state_manager.py tests/schedule/test_task_allocator.py
git commit -m "feat(control): route heuristic tasks and filter scheduling ownership"
```

---

### Task 10: Coordinator and event delivery

**Files:**
- Create: `src/control/common/coordinator.py`
- Create: `tests/control/test_coordinator.py`

**Interfaces:**
- Produces: `ControlTickResult`.
- Produces: `ControlCoordinator.queue_event()`, `start_work()`, `assign_task()`, `step_uav()`, `revoke_for_return()`, `current_lease()`.

- [ ] **Step 1: Write coordinator tests**

Test one act call per UAV/tick, ordered event delivery, one-step event visibility, same-tick heuristic replacement before `act()`, stale lease rejection, controller event sequencing, safety result recording, learning lease persistence, and invalid-command emergency threshold.

```python
def test_event_created_after_control_is_visible_next_tick(coordinator, uav):
    first = coordinator.step_uav(uav, current_time=1.0)
    coordinator.queue_event(ControlEvent(1, 1.0, "weather_updated", "env", uav.id, {}))
    second = coordinator.step_uav(uav, current_time=2.0)

    assert first.observation.events == ()
    assert [event.event_type for event in second.observation.events] == ["weather_updated"]
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/control/test_coordinator.py -q`

Expected: collection ERROR for missing coordinator.

- [ ] **Step 3: Implement coordinator registries**

Maintain dictionaries keyed by UAV ID for configured control mode, controller, pending task, queued events, invalid-command count, last applied command, operation mode, safety flag and last tick time. `start_work()` acquires HEURISTIC or LEARNING based on configured mode and creates `episode_id = f"{uav_id}:{sortie_number}"`. `assign_task()` is legal only for HEURISTIC mode. `revoke_for_return(recovery_plan)` accepts only a reserved, validated `RecoveryPlan`, releases the current lease and installs a system-owned `ReturnToBaseController` command source with a pending RETURN task. Replacing the command source never changes `_configured_modes[uav_id]`.

Define:

```python
@dataclass(frozen=True)
class ControlTickResult:
    lease: ControlLease
    observation: ControlObservation
    decision: ControlDecision
    safety: SafetyResult
    execution: ExecutionResult
    emitted_events: tuple[ControlEvent, ...]
```

- [ ] **Step 4: Implement one control tick**

In exact order:

```python
events = self._take_queued_events(uav.id, current_time)
remaining_events = self._apply_heuristic_transitions(uav.id, events, current_time)
lease = self.ownership.current(uav.id)
controller = self._controllers[uav.id]
observation = self.observations.build(
    uav,
    self.state_manager,
    events=remaining_events,
    bases=self._base_observations(),
    control_mode=self._configured_modes[uav.id],
    control_owner=lease.owner,
    operation_mode=self._operation_modes[uav.id],
    safety_intervened=self._last_safety_intervened[uav.id],
    current_time=current_time,
    dt_min=dt_min,
)
pending_task = self._pending_tasks.pop(uav.id, None)
if pending_task is not None:
    if not isinstance(controller, HeuristicControllerBase):
        raise ControlCoordinatorError("only task controllers accept ControlTask")
    context = self._controller_context(uav.id, pending_task, dt_min)
    controller.reset(context)
    controller.start_task(pending_task, observation)
decision = controller.act(observation)
if not self.ownership.accepts(lease):
    current = self.ownership.current(uav.id)
    raise StaleControlCommand(
        f"stale command for {uav.id}: expected generation "
        f"{lease.generation}, current generation {current.generation}"
    )
safety = self.safety.apply(decision.command, observation, dt_min)
invalid_streak = self._update_invalid_streak(safety.interventions)
if invalid_streak >= self.config.safety.max_invalid_commands:
    raise EmergencyRevokeRequired(uav.id, invalid_streak)
execution = self.executor.execute(uav, safety.applied_command, dt_min)
self.operation_registry.reconcile(
    uav.id,
    self._last_applied_commands.get(uav.id),
    safety.applied_command,
    observation,
)
self._operation_modes[uav.id] = safety.applied_command.operation_mode
self._last_applied_commands[uav.id] = safety.applied_command
emitted = self._queue_decision_events(uav.id, decision.events, current_time)
return ControlTickResult(lease, observation, decision, safety, execution, emitted)
```

Snapshot and clear queued events before task-flow processing. `_apply_heuristic_transitions()` must update both the controller registry and pending task before the lease/controller are re-read. A heuristic transition event is consumed by task flow and is not delivered to either old or new controller. Non-transition events appear once in the current observation. LEARNING bypasses task flow, so all normal task events appear once in its observation. Events emitted by the decision or queued later in the simulation step remain for the next tick.

- [ ] **Step 5: Implement bounded fault handling**

Increment invalid-command count for a rejected schema/mask/non-finite command and for every returned `SafetyResult` with non-empty interventions, including successful clipping or collision correction. Reset it only after a command with no intervention. The threshold check occurs after `safety.apply()` but before `executor.execute()`; the threshold command must not change pose, range, sensor binding or operation registry. Add tests for three consecutive clipped commands, unchanged pose/range on the third command, and reset by one clean command. Do not automatically choose a heuristic task fallback for BC/RL.

- [ ] **Step 6: Run coordinator suite**

Run: `python -m pytest tests/control/test_coordinator.py tests/control/test_ownership.py tests/control/test_safety.py tests/control/test_executor.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/control/common/coordinator.py tests/control/test_coordinator.py
git commit -m "feat(control): coordinate per-UAV observe act execution"
```

---

### Task 11: Integrate the control runtime into SimulationEngine

**Files:**
- Modify: `src/env/simulation.py`
- Modify: `src/env/uav_entity.py`
- Modify: `tests/env/test_simulation_integration.py`
- Modify: `tests/env/test_storm_avoidance.py`
- Create: `tests/control/test_simulation_ownership.py`

**Interfaces:**
- `SimulationEngine.control_coordinator: ControlCoordinator`.
- Existing external `SimulationEngine.step() -> dict` remains unchanged.

- [ ] **Step 1: Add integration tests before changing the engine**

Cover default heuristic startup, search-to-track ownership replacement, work-range revocation, controller exception/three-invalid-command recovery, dynamic-obstacle replan during SYSTEM return, and the fact that target events do not replace a fake learning controller lease:

```python
def test_detection_replaces_heuristic_task_without_system_command():
    engine = SimulationEngine(ConfigLoader.load(), seed=9)
    engine.step()
    uav = next(
        item for item in engine.uavs
        if engine.control_coordinator.current_lease(item.id).owner is ControlOwner.HEURISTIC
    )
    old = engine.control_coordinator.current_lease(uav.id)

    engine._handle_detection(uav, engine.ships[0], engine.clock.time)
    engine.step()
    new = engine.control_coordinator.current_lease(uav.id)

    assert old.owner is ControlOwner.HEURISTIC
    assert new.owner is ControlOwner.HEURISTIC
    assert new.generation == old.generation + 1
    assert new.controller_id.startswith("tracking:")
```

- [ ] **Step 2: Run integration tests and verify failure**

Run: `python -m pytest tests/control/test_simulation_ownership.py -q`

Expected: FAIL because `SimulationEngine` has no `control_coordinator`.

- [ ] **Step 3: Construct the runtime during engine initialization**

Extend the constructor with optional keyword-only `control_providers: Mapping[ControlMode, ControllerProvider] | None = None` (or an injected preconfigured `ControlFactory`). Create `ControlOwnership`, `ObservationProvider`, `SafetyEnvelope`, `UAVDynamicsExecutor`, `ControlFactory`, and `ControlCoordinator` after UAV/sensor construction; register injected providers before resolving and validating any selected mode. Resolve each UAV mode from `per_uav.get(uav.id, default_mode)`. This is the supported external plugin entry; default `None` preserves the existing constructor API.

- [ ] **Step 4: Replace direct UAV stepping**

In `SimulationEngine.step()`, replace `uav.step(target_position=target)` for task-controlled aircraft with `control_coordinator.step_uav()`. Keep system return/refuel/holding paths under SYSTEM ownership, but route return motion through the same safety and executor path.

Before issuing the next task command, call `RecoveryPlanner.evaluate()` against the current global planning mask/version and all `BaseObservation` values. Work range is exhausted when the shortest validated candidate satisfies `remaining_range <= candidate.path_length_cells + candidate.reserve_cells + max_speed_cells_min * dt_min`. Recheck the chosen base load, reserve it in `_return_base_by_uav`, create `RecoveryPlan` with a unique reservation ID, and only then call `revoke_for_return(recovery_plan)`. If no validated candidate exists, emit `no_safe_recovery_path` and raise `NoSafeRecoveryPath`; do not revoke first or execute an unvalidated chord.

Use the same recovery transaction only for faults while the current lease owner is HEURISTIC or LEARNING: catch production-mode controller exceptions, `EmergencyRevokeRequired` and unrecoverable `UnsafeControlState`, record `controller_fault` or `invalid_command_limit`, evaluate paths, reserve a base, then atomically revoke to SYSTEM return. Dispatch exceptions by current owner/operation before the generic controller-exception branch. A `NoSafeRecoveryPath` raised while owner is SYSTEM and operation is RETURN goes directly to emergency failure, preserving the existing reservation and never evaluating another base; SYSTEM/HOLDING controller faults also fail directly. If work-controller validation/reservation fails, enter explicit emergency failure without applying the failed command. Tests must assert that exception/invalid paths never install `ReturnToBaseController` before a valid reservation exists and that the threshold command leaves pose/range unchanged.

While returning, pass every observation map version to `ReturnToBaseController`. On version change it validates the unflown suffix; if blocked, it replans to the already reserved base, retains that reservation, and accepts the new route only when its measured length plus reserve fits remaining range. `NoSafeRecoveryPath` from this process enters emergency failure and never falls back to another base or a straight chord.

- [ ] **Step 5: Replace direct task-control calls with events**

Modify these flows:

- `_handle_detection()` records target report and queues `target_found`; it no longer calls `uav.start_tracking()`.
- `_release_target_group()` queues release events; it no longer calls `cancel_tracking()` or `_resume_search()` for learning-owned UAVs.
- `_lose_target_to_storm()` queues `target_lost`; heuristic task flow handles the switch.
- Remove `_process_search_completions()` as an event consumer in the new path. `search_complete` is consumed exclusively by `HeuristicTaskFlow`; any remaining legacy bookkeeping receives the resulting `TaskTransition` and must not read or remove the event queue.
- `_replan_conflicting_routes()` queues `route_blocked` for task controllers instead of directly assigning a route.

Keep legacy entity methods import-compatible, but remove every production call to their task-switching branches. Characterization tests must be migrated to the coordinator rather than selecting a second runtime path.

- [ ] **Step 6: Sync control state into StateManager**

Extend `_sync_state_from_entities()` to publish the current lease, persisted configured control mode, operation mode, generation and safety intervention. Never derive configured mode from a SYSTEM return/holding controller class. Continue populating legacy `status`, `sensor_mode`, `assigned_region_id` and `target_group_id` for schedule and UI compatibility.

- [ ] **Step 7: Migrate existing integration tests**

Replace tests that mutate `_wp_index` or call `_update_route_state()` with controller phase/command assertions. Preserve the behavioral claims: SAR off during turns, SAR only on stable legs, safe dynamic replan, tracking phase coordination, target loss, return, refuel and lifecycle completion.

- [ ] **Step 8: Run simulation regressions**

Run: `python -m pytest tests/control/test_simulation_ownership.py tests/env/test_simulation_integration.py tests/env/test_storm_avoidance.py tests/env/test_ais_discrimination.py -q`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/env/simulation.py src/env/uav_entity.py tests/control/test_simulation_ownership.py tests/env/test_simulation_integration.py tests/env/test_storm_avoidance.py
git commit -m "refactor(control): run simulation through control coordinator"
```

---

### Task 12: Public API, visualization diagnostics, compatibility, and full verification

**Files:**
- Modify: `src/control/__init__.py`
- Modify: all four control package `__init__.py` files
- Modify: `src/vis/backend/frame_builder.py`
- Modify: `src/vis/backend/server.py`
- Modify: `tests/env/test_goal2_foundation.py`
- Modify: `tests/env/test_server_runtime.py`
- Modify: `README.md`

**Interfaces:**
- Public imports for common contracts and the three base classes.
- Frame fields: `control_mode`, `control_owner`, `operation_mode`, `controller_generation`, `safety_intervened`.

- [ ] **Step 1: Write frame and API tests**

Add assertions to the existing backend tests:

```python
uav_frame = frame["uavs"][0]
assert uav_frame["control_mode"] == "heuristic"
assert uav_frame["control_owner"] in {"system", "heuristic", "learning"}
assert isinstance(uav_frame["controller_generation"], int)
assert isinstance(uav_frame["safety_intervened"], bool)
assert api_config["control"]["default_mode"] == "heuristic"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/env/test_goal2_foundation.py tests/env/test_server_runtime.py -q`

Expected: FAIL because frame/config control fields are absent.

- [ ] **Step 3: Publish stable package APIs**

Re-export:

```python
from src.control.bc.base import BCControllerBase
from src.control.heuristic.base import HeuristicControllerBase
from src.control.rl.base import RLControllerBase
from src.control.common.contracts import ControlCommand, ControlObservation

__all__ = [
    "BCControllerBase",
    "ControlCommand",
    "ControlObservation",
    "HeuristicControllerBase",
    "RLControllerBase",
]
```

Each subpackage exports only its supported public classes; internal phases and helpers remain private.

- [ ] **Step 4: Add diagnostics without changing existing fields**

Append the five control fields to each UAV frame object. Add the complete `control` block to `/api/config`. Do not rename or remove `status`, `sensor_mode`, paths, footprints or current replay fields.

- [ ] **Step 5: Update README architecture and migration notes**

Document the new directory structure, default heuristic mode, control ownership distinction, A* + Dubins navigation, BC/RL abstract-only status, and fail-fast behavior for unregistered learning modes. Remove statements claiming that active cross-region navigation is RRT*.

- [ ] **Step 6: Run focused backend and control tests**

Run: `python -m pytest tests/control tests/env/test_goal2_foundation.py tests/env/test_server_runtime.py -q`

Expected: PASS.

- [ ] **Step 7: Run the complete Python test suite**

Run: `python -m pytest -q`

Expected: PASS with no failures or collection errors.

- [ ] **Step 8: Run static repository checks**

Run: `python -m compileall -q src tests`

Expected: exit code 0.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 9: Commit**

```bash
git add src/control src/vis/backend/frame_builder.py src/vis/backend/server.py tests/env/test_goal2_foundation.py tests/env/test_server_runtime.py README.md
git commit -m "docs(control): publish control strategy runtime interface"
```

---

## Implementation Review Gates

After Task 2, review the contracts before writing runtime code. In particular, reject any interface that exposes `SimulationEngine`, `Ship`, or mutable state objects to a controller.

After Task 6, review A* paths visually or with saved coordinate traces for at least one direct route, one obstacle detour and one unreachable case. Confirm A* selects the route and Dubins only converts it to a flyable trajectory.

After Task 10, review ownership invariants before integrating the simulation. No task event may mutate a learning lease.

After Task 11, compare fixed-seed heuristic behavior against existing coverage, tracking, storm and lifecycle expectations. Differences caused by A* are allowed only when paths remain safe and task outcomes remain equivalent.

## Final Acceptance Checklist

- [ ] All four control directories exist and imports are acyclic.
- [ ] All three implementation directories expose a `base.py`.
- [ ] BC/RL contain abstract definitions only.
- [ ] Current four `src/control` functions delegate to heuristic implementations with deprecation warnings.
- [ ] Default simulation uses the coordinator and heuristic controllers.
- [ ] TaskAllocator cannot assign a learning-owned UAV.
- [ ] A normal task event cannot revoke or replace a learning lease.
- [ ] Work-range exhaustion reserves enough distance for a system return.
- [ ] A* paths avoid obstacles, prohibit corner cutting and pass Dubins safety validation.
- [ ] Observation tests prove hidden ship truth is absent.
- [ ] Safety intervention preserves task intent and is visible in frames.
- [ ] Existing public frame fields remain backward compatible.
- [ ] `python -m pytest -q` passes.
- [ ] `python -m compileall -q src tests` passes.
- [ ] `git diff --check` is clean.
