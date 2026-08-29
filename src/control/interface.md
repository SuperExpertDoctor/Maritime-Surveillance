# UAV Control Strategy Interfaces

本文档说明 `src/control` 的控制策略接口、运行时契约和自定义控制器的集成方式。
文档对应当前实现的 `control-observation/v1` 与 `control-command/v1` 契约。

## 1. 架构边界

每架 UAV 的控制命令必须经过同一条运行时链路：

```text
环境 / 传感器 / 调度器
          |
          v
ObservationProvider  -- 只发布已观测和已发布的快照
          |
          v
ControlCoordinator    -- 每架 UAV 独立编排
          |
          +--> ControllerBase.act(observation)
          |
          v
SafetyEnvelope        -- 校验、裁剪、避障和传感器约束
          |
          v
UAVDynamicsExecutor   -- 唯一的本机动力学执行入口
          |
          v
UAVEntity
```

控制器只负责根据 `ControlObservation` 产生一个 `ControlDecision`，不能直接依赖
`SimulationEngine`、`UAVEntity`、`Ship` 或 `StateManager`，也不能直接修改这些对象。
安全层可以修正速度、转弯率、传感器和碰撞风险，但不替控制器选择任务、目标或
operation mode。

当前四个控制目录的职责如下：

| 目录 | 作用 | 当前内容 |
| --- | --- | --- |
| `src/control/common/` | 公共基类、数据契约、观测、安全、执行、lease、协调和工厂 | 所有策略都依赖的稳定边界 |
| `src/control/heuristic/` | 单任务、事件驱动的启发式控制 | 覆盖、跟踪、返航、holding、Hybrid A* |
| `src/control/bc/` | 行为克隆抽象 | 只有 `BCControllerBase`，不含模型 |
| `src/control/rl/` | 强化学习抽象 | 只有 `RLControllerBase`，不含模型或训练代码 |

推荐从稳定入口导入：

```python
from src.control import (
    BCControllerBase,
    ControlCommand,
    ControlObservation,
    HeuristicControllerBase,
    RLControllerBase,
)
from src.control.common import (
    ActionSpec,
    ControlDecision,
    ControlMode,
    ControlTask,
    ControllerContext,
    ObservationSpec,
    OperationMode,
    PolicySource,
    SensorMode,
    StopReason,
)
```

## 2. 三种模式和控制权

`ControlMode`、`ControlOwner` 和 `OperationMode` 是三个不同概念：

| 概念 | 枚举值 | 含义 |
| --- | --- | --- |
| `ControlMode` | `heuristic`、`bc`、`rl` | UAV 的配置策略类型 |
| `ControlOwner` | `system`、`heuristic`、`learning` | 当前 tick 谁持有控制 lease |
| `OperationMode` | `idle`、`transit`、`coverage`、`track`、`return`、`holding` | 当前动作执行的作业模式 |

例如，配置为 `rl` 的 UAV 在低油量返航时仍然保持
`self_state.control_mode == ControlMode.RL`，但当前 lease 会临时转为
`ControlOwner.SYSTEM`，动作的 `operation_mode` 为 `RETURN`。不能根据
`operation_mode` 推导策略类型，也不能在返航时把配置模式改成 `heuristic`。

控制权使用不可伪造的 `ControlLease` 和递增 `generation`：

- heuristic lease 以一个任务为边界，覆盖、跟踪等任务切换由事件流触发并原子替换。
- BC/RL 使用 `ControlOwner.LEARNING`，从 sortie 开始持续到工作里程耗尽；普通
  目标发现、任务完成等事件只进入观测，不会抢回 learning lease。
- 返航、holding、低油量和控制故障使用显式 SYSTEM lease。
- 控制器做出决策后，协调器会再次检查 lease generation；若控制权已经变化，命令
  会以 `StaleControlCommand` 拒绝，不会执行过期动作。

## 3. 公共数据契约

所有公共数据类型位于 `src/control/common/contracts.py`，主要使用冻结 dataclass。
数组在创建 `ControlObservation` 时复制并设为只读，集合字段会转为 tuple，事件和
policy metadata 的映射会转为不可变快照。

### 3.1 `ObservationSpec` 和 `ActionSpec`

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
```

当前默认观测 schema 是 `control-observation/v1`，局部窗口来自
`configs/control.yaml`，默认大小为 `11 x 11`。动作使用仿真物理单位：转弯率是
`rad/min`，速度是 `cells/min`，不是模型常见的 `[-1, 1]` 归一化值。

应用启动时，`SimulationEngine` 会根据巡航速度和安全速度比例生成实际
`ActionSpec`，并把同一个规格传给内置控制器和外部 provider。自定义学习控制器的
`action_spec` 必须与运行时的版本和范围一致；模型归一化输出应在控制器自己的
`decode_action` 或 `predict_action` 中转换为物理单位。

### 3.2 `ControlObservation`

完整结构如下：

```python
@dataclass(frozen=True)
class ControlObservation:
    schema_version: str
    timestamp_min: float
    dt_min: float
    self_state: UAVObservation
    local_info: np.ndarray
    local_value: np.ndarray
    obstacle_mask: np.ndarray
    searchable_mask: np.ndarray
    planning_obstacle_mask: np.ndarray
    planning_map_version: int
    contacts: tuple[ContactObservation, ...]
    hazards: tuple[HazardObservation, ...]
    bases: tuple[BaseObservation, ...]
    shared_uavs: tuple[UAVObservation, ...]
    events: tuple[ControlEvent, ...]
    action_mask: ActionMask
```

字段语义：

| 字段 | 内容和使用约束 |
| --- | --- |
| `timestamp_min` | 当前仿真时间，单位为分钟 |
| `dt_min` | 当前控制步长，必须为有限正数 |
| `self_state` | 当前 UAV 的公开状态、配置模式、控制权、作业模式和上一步安全介入标志 |
| `local_info` | 以 UAV 为中心的局部信息场快照，默认 `float32` |
| `local_value` | 以 UAV 为中心的局部信息价值快照，默认 `float32` |
| `obstacle_mask` | 局部障碍 mask，`bool`，窗口外用障碍填充 |
| `searchable_mask` | 局部可搜索 mask，`bool` |
| `planning_obstacle_mask` | 全局规划 mask，只含已发布的陆地和障碍物占用，`bool` |
| `planning_map_version` | 全局规划 mask 的版本；变化后路径需要重新验证 |
| `contacts` | 当前由传感器/状态管理器发布的目标估计，不是舰船真值 |
| `hazards` | 岛屿、雷暴等已发布危险物的几何和运动快照 |
| `bases` | 基地位置、容量和已预留维护负载 |
| `shared_uavs` | 其他 UAV 的公开状态，用于协同和避碰 |
| `events` | 本 UAV 当前 tick 消费到的事件快照，事件只消费一次 |
| `action_mask` | 当前控制权和观测资源允许的传感器、作业模式、目标 ID |

`planning_obstacle_mask` 是全局规划数据，不是环境对象引用，也不包含舰船位置。
`contacts` 中没有有效 contact 时，控制器不能自行猜测目标；必须使用空 contact 集合
和 action mask 表达“当前没有可跟踪目标”。

`ActionMask` 的主要规则：

- `HEURISTIC` 或 `LEARNING` 作业 lease 通常允许 `TRANSIT`、`COVERAGE`，有有效
  contact 时才允许 `TRACK` 和 `EO`。
- `SYSTEM` 的 `RETURN`/`HOLDING` 只允许 `OFF` 传感器和对应系统作业模式。
- `target_contact_ids` 只列出本帧 `contacts` 的 ID。
- SAR 是否能够成像还会由安全层依据作业模式和转弯率判断；不能只依赖 mask。

### 3.3 动作和事件

```python
@dataclass(frozen=True)
class ControlCommand:
    turn_rate_rad_min: float
    speed_cells_min: float
    sensor_mode: SensorMode
    operation_mode: OperationMode
    target_contact_id: str | None = None
    schema_version: str = "control-command/v1"


@dataclass(frozen=True)
class ControlDecision:
    command: ControlCommand
    events: tuple[ControllerEventRequest, ...] = ()
```

一个动作只描述一个仿真控制步，不是整条航路。控制器可以在内部保留路线、滤波器
或策略状态，但每个 tick 必须返回 `ControlDecision`。

控制器事件使用 `ControllerEventRequest` 请求；协调器会分配全局 sequence、设置
时间戳和来源，并把它们排入后续 tick。控制器不能直接修改事件队列或调用调度器。
外部环境事件使用 `ControlEvent` 通过 `ControlCoordinator.queue_event()` 进入同一
队列。

典型规则：

```python
return ControlDecision(
    command=ControlCommand(
        turn_rate_rad_min=turn_rate,
        speed_cells_min=speed,
        sensor_mode=SensorMode.SAR,
        operation_mode=OperationMode.COVERAGE,
    ),
    events=(
        ControllerEventRequest(
            "search_complete",
            {"task_id": task.task_id},
        ),
    ),
)
```

动作中的 `TRACK` 必须同时提供 `target_contact_id`，且该 ID 必须存在于本帧
`action_mask.target_contact_ids`。`OperationRegistry` 只登记已应用动作产生的
跟踪区域和传感器绑定，不负责生成下一条飞行命令。

## 4. 公共控制器基类

### 4.1 `ControllerBase`

文件：`src/control/common/base.py`

```python
class ControllerBase(ABC):
    @property
    @abstractmethod
    def control_mode(self) -> ControlMode: ...

    @property
    @abstractmethod
    def observation_spec(self) -> ObservationSpec: ...

    @property
    @abstractmethod
    def action_spec(self) -> ActionSpec: ...

    def reset(self, context: ControllerContext) -> None: ...

    @abstractmethod
    def act(self, observation: ControlObservation) -> ControlDecision: ...

    def close(self) -> None: ...
```

`reset()` 默认只保存 `context`；策略需要清理内部状态时可以覆盖，但应先调用
`super().reset(context)`。`close()` 默认无操作，适合释放模型会话、文件句柄或外部
资源。所有子类都必须实现 `control_mode`、`observation_spec`、`action_spec` 和
`act()`。

`ControllerContext` 提供当前 UAV ID、控制步长、规格、episode ID 和可选任务：

```python
@dataclass(frozen=True)
class ControllerContext:
    uav_id: str
    dt_min: float
    observation_spec: ObservationSpec
    action_spec: ActionSpec
    episode_id: str
    task: ControlTask | None = None
```

### 4.2 `LearningControllerBase`

文件：`src/control/common/base.py`

学习控制器还必须实现：

```python
class LearningControllerBase(ControllerBase):
    @property
    def ownership_scope(self) -> str:
        return "sortie"

    @abstractmethod
    def load_policy(self, source: PolicySource) -> None: ...
```

`ownership_scope` 默认是 `sortie`，表示同一 sortie 内持续持有 learning lease。
`PolicySource` 只有 URI 和不可变 metadata，模型文件的具体格式由子类决定。

## 5. BC 接口

文件：`src/control/bc/base.py`。公开入口是 `src.control.BCControllerBase`。

### 5.1 接口定义

```python
class BCControllerBase(LearningControllerBase):
    @property
    def control_mode(self) -> ControlMode:
        return ControlMode.BC

    def act(self, observation: ControlObservation) -> ControlDecision:
        encoded = self.encode_observation(observation)
        output = self.predict_action(encoded)
        return ControlDecision(command=self.decode_action(output))

    @abstractmethod
    def encode_observation(self, observation: ControlObservation) -> object: ...

    @abstractmethod
    def predict_action(self, encoded_observation: object) -> object: ...

    @abstractmethod
    def decode_action(self, model_output: object) -> ControlCommand: ...
```

BC 基类已经实现了 `act()` 模板方法，调用顺序固定为：

```text
ControlObservation -> encode_observation -> predict_action -> decode_action
```

通常只需要实现模型加载、观测编码、单次推理和动作解码。`predict_action` 可以返回
任意模型输出；但 `decode_action` 必须最终返回 `ControlCommand`，不能返回字典、
NumPy 数组或模型的归一化向量。

### 5.2 BC 自定义子类示例

下面示例只展示接口适配，不限定 PyTorch、ONNX 或其他模型框架：

```python
from pathlib import Path

import numpy as np

from src.control import BCControllerBase, ControlCommand, ControlObservation
from src.control.common import (
    ActionSpec,
    ControlMode,
    ObservationSpec,
    PolicySource,
    OperationMode,
    SensorMode,
)


class MyBCController(BCControllerBase):
    def __init__(
        self,
        observation_spec: ObservationSpec,
        action_spec: ActionSpec,
    ) -> None:
        self._observation_spec = observation_spec
        self._action_spec = action_spec
        self._model = None

    @property
    def observation_spec(self) -> ObservationSpec:
        return self._observation_spec

    @property
    def action_spec(self) -> ActionSpec:
        return self._action_spec

    def load_policy(self, source: PolicySource) -> None:
        # Replace this with the project's model loader.
        self._model = load_model(Path(source.uri))

    def encode_observation(self, observation: ControlObservation) -> np.ndarray:
        if observation.schema_version != self.observation_spec.schema_version:
            raise ValueError("unsupported observation schema")
        return np.stack(
            [
                observation.local_info,
                observation.local_value,
                observation.obstacle_mask.astype(np.float32),
                observation.searchable_mask.astype(np.float32),
            ],
            axis=0,
        )

    def predict_action(self, encoded_observation: np.ndarray) -> object:
        if self._model is None:
            raise RuntimeError("load_policy must be called before act")
        return self._model.predict(encoded_observation)

    def decode_action(self, model_output: object) -> ControlCommand:
        turn_rate, speed = map(float, model_output[:2])
        # The model output is assumed to already be converted to physical units.
        return ControlCommand(
            turn_rate_rad_min=turn_rate,
            speed_cells_min=speed,
            sensor_mode=SensorMode.OFF,
            operation_mode=OperationMode.TRANSIT,
        )


def load_model(path: Path) -> object:
    raise NotImplementedError("connect the project's real model loader here")
```

生产代码中应把 `load_model` 换成实际加载逻辑；示例中的
`NotImplementedError` 不是可运行的控制器。BC 控制器若需要产生事件，可以覆盖
`act()` 并返回带 `events` 的 `ControlDecision`，但仍必须保持动作和事件契约。

### 5.3 BC 的 episode 生命周期

`ControlCoordinator.start_work()` 创建 BC controller 时会：

1. 通过 provider 构造一个新的 controller。
2. 校验 `control_mode == ControlMode.BC` 和 observation schema。
3. 调用 `reset(ControllerContext(...))`。
4. 获取观测并在每个 tick 调用 `act()`。
5. 在普通目标、搜索和跟踪事件到来时保持 learning lease。
6. 仅在安全故障、显式低油量/里程返航或其他 SYSTEM 生命周期条件下转移控制权。

`load_policy()` 不会由当前 `ControlCoordinator` 自动调用。应用应在 provider 构造
controller 后加载策略，或者让 provider 返回一个已经完成加载的实例。

## 6. RL 接口

文件：`src/control/rl/base.py`。公开入口是 `src.control.RLControllerBase`。

### 6.1 接口定义

```python
class RLControllerBase(LearningControllerBase):
    @property
    def control_mode(self) -> ControlMode:
        return ControlMode.RL

    def reset(self, context: ControllerContext) -> None:
        super().reset(context)
        self._policy_state = self.initial_policy_state()
        self._deterministic = True
        self.reset_episode(context.episode_id)

    def act(self, observation: ControlObservation) -> ControlDecision:
        command, self._policy_state = self.predict_action(
            observation,
            self._policy_state,
            self._deterministic,
        )
        return ControlDecision(command=command)

    def set_evaluation_mode(self, enabled: bool) -> None:
        self._deterministic = enabled

    @property
    @abstractmethod
    def observation_space(self) -> object: ...

    @property
    @abstractmethod
    def action_space(self) -> object: ...

    @abstractmethod
    def initial_policy_state(self) -> object | None: ...

    @abstractmethod
    def predict_action(
        self,
        observation: ControlObservation,
        policy_state: object | None,
        deterministic: bool,
    ) -> tuple[ControlCommand, object | None]: ...

    @abstractmethod
    def reset_episode(self, episode_id: str) -> None: ...
```

`RLControllerBase` 管理 recurrent policy state 和 episode reset。每个 tick 的
`predict_action` 必须返回 `(ControlCommand, next_policy_state)`；不可只返回动作。
`set_evaluation_mode(True)` 使后续推理使用确定性模式，`False` 允许策略使用随机性。
`observation_space` 和 `action_space` 是适配器元数据，不要求本轮引入 Gymnasium。

### 6.2 RL 自定义子类示例

```python
from src.control import RLControllerBase, ControlCommand, ControlObservation
from src.control.common import (
    ActionSpec,
    ObservationSpec,
    PolicySource,
    OperationMode,
    SensorMode,
)


class MyRLController(RLControllerBase):
    def __init__(self, observation_spec, action_spec) -> None:
        self._observation_spec = observation_spec
        self._action_spec = action_spec
        self._policy = None

    @property
    def observation_spec(self) -> ObservationSpec:
        return self._observation_spec

    @property
    def action_spec(self) -> ActionSpec:
        return self._action_spec

    @property
    def observation_space(self) -> object:
        return {"local_window": self.observation_spec.local_window_cells}

    @property
    def action_space(self) -> object:
        return {
            "turn_rate_rad_min": (
                self.action_spec.min_turn_rate_rad_min,
                self.action_spec.max_turn_rate_rad_min,
            ),
            "speed_cells_min": (
                self.action_spec.min_speed_cells_min,
                self.action_spec.max_speed_cells_min,
            ),
        }

    def load_policy(self, source: PolicySource) -> None:
        self._policy = load_rl_policy(source.uri)

    def initial_policy_state(self) -> object | None:
        return None

    def reset_episode(self, episode_id: str) -> None:
        if self._policy is not None:
            self._policy.reset(episode_id)

    def predict_action(self, observation, policy_state, deterministic):
        if self._policy is None:
            raise RuntimeError("load_policy must be called before act")
        model_output, next_state = self._policy.act(
            observation,
            policy_state,
            deterministic=deterministic,
        )
        turn_rate, speed = map(float, model_output[:2])
        command = ControlCommand(
            turn_rate,
            speed,
            SensorMode.OFF,
            OperationMode.TRANSIT,
        )
        return command, next_state


def load_rl_policy(uri: str) -> object:
    raise NotImplementedError("connect the project's real RL policy loader here")
```

与 BC 一样，示例中的模型加载占位函数必须替换为真实实现。RL controller 不应在
`predict_action` 中修改环境对象；策略内部状态只能用于下一次策略推理。

## 7. 启发式接口

文件：`src/control/heuristic/base.py`。启发式控制是单任务控制，不是一个持续的
端到端 episode。它使用 `ControlTask` 开始任务，用 `ControlDecision.events` 请求
任务完成/失败事件。

### 7.1 接口定义

```python
class HeuristicControllerBase(ControllerBase):
    @property
    def control_mode(self) -> ControlMode:
        return ControlMode.HEURISTIC

    @property
    @abstractmethod
    def operation_mode(self) -> OperationMode: ...

    @abstractmethod
    def start_task(
        self,
        task: ControlTask,
        observation: ControlObservation,
    ) -> None: ...

    @abstractmethod
    def is_complete(self, observation: ControlObservation) -> bool: ...

    @abstractmethod
    def stop_task(self, reason: StopReason) -> None: ...
```

除此之外，必须实现 `ControllerBase` 要求的 `observation_spec`、`action_spec` 和
`act()`。生命周期如下：

```text
factory.create_heuristic(..., task)
        |
coordinator.assign_task / start_work
        |
next tick: reset(context) -> start_task(task, observation) -> act(observation)
        |
subsequent ticks: act(observation)
        |
completion/failure event -> task_flow consumes once -> atomic replacement
        |
stop_task(reason)  (清理资源，不再产生新动作)
```

启发式实现必须保证 `stop_task()` 幂等；它只做资源清理，不能在被抢占后再提交一条
动作。路径规划器可以保留路线，但真正推进 UAV 只能通过返回 `ControlCommand`。

### 7.2 启发式自定义子类示例

```python
from src.control import HeuristicControllerBase, ControlCommand, ControlObservation
from src.control.common import (
    ActionSpec,
    ControlDecision,
    ControllerEventRequest,
    ControlTask,
    ObservationSpec,
    OperationMode,
    SensorMode,
    StopReason,
)


class MyCoverageController(HeuristicControllerBase):
    def __init__(self, observation_spec, action_spec) -> None:
        self._observation_spec = observation_spec
        self._action_spec = action_spec
        self._task = None
        self._complete = False

    @property
    def observation_spec(self) -> ObservationSpec:
        return self._observation_spec

    @property
    def action_spec(self) -> ActionSpec:
        return self._action_spec

    @property
    def operation_mode(self) -> OperationMode:
        return OperationMode.COVERAGE

    def start_task(self, task: ControlTask, observation: ControlObservation) -> None:
        del observation
        if task.task_type is not OperationMode.COVERAGE:
            raise ValueError("MyCoverageController requires a COVERAGE task")
        if task.region_bbox is None:
            raise ValueError("coverage task requires region_bbox")
        self._task = task
        self._complete = False

    def act(self, observation: ControlObservation) -> ControlDecision:
        if self._task is None:
            raise RuntimeError("start_task must be called before act")

        # Replace this with the route follower or guidance law.  The result
        # must remain one-step physical control, not a UAVEntity mutation.
        command = ControlCommand(
            turn_rate_rad_min=0.0,
            speed_cells_min=self.action_spec.max_speed_cells_min,
            sensor_mode=SensorMode.SAR,
            operation_mode=OperationMode.COVERAGE,
        )
        if self.is_complete(observation):
            return ControlDecision(
                command,
                events=(
                    ControllerEventRequest(
                        "search_complete",
                        {"task_id": self._task.task_id},
                    ),
                ),
            )
        return ControlDecision(command)

    def is_complete(self, observation: ControlObservation) -> bool:
        del observation
        return self._complete

    def stop_task(self, reason: StopReason) -> None:
        del reason
        self._task = None

```

示例中的 `ControllerEventRequest` 需要从
`src.control.common.contracts` 导入。真实实现还应在自己的航路/覆盖进度达到终点
时设置 `_complete`，并确保完成事件只发一次。

### 7.3 当前启发式工厂边界

当前 `ControlFactory` 内置并公开导出的启发式 controller 是：

- `CoverageController`：Hybrid A* 转场后执行 SAR 蛇形扫描。
- `TrackingController`：根据已观测 contact 进行 A* 接近和 EO/IR standoff 盘旋。
- `ReturnToBaseController`：执行已预留、已验证的安全返航路线。
- `SystemHoldingController`：SYSTEM lease 下保持安全盘旋。

`ControlFactory.register()` 只用于注册 `BC` 或 `RL` provider，故意不允许用一个
普通 provider 替换 heuristic 根 provider。这样可以保护任务流、返航和 SYSTEM 安全
路径不被误配置覆盖。

因此，在当前版本集成自定义启发式子类需要先增加一个明确的工厂适配层：

1. 继承 `HeuristicControllerBase`，保持上面的 task/lifecycle 契约。
2. 在 `src/control/common/factory.py` 为对应的现有 `OperationMode` 增加显式映射，
   或定义项目自己的 `ControlFactory` 子类覆盖任务到 controller 的构造映射。
3. 让运行时使用该 factory 构造 `ControlCoordinator`，而不是在
   `SimulationEngine` 或 `UAVEntity` 中直接实例化 controller。
4. 为新映射补充 factory、coordinator、事件单次消费、动作安全和实体集成测试。

当前安全边界不允许通过自定义 heuristic provider 替换 `RETURN`/`HOLDING` 的系统
控制路径；若确实需要改变系统返航逻辑，必须同时重新验证 reservation、地图版本、
路径终点、返航余量和无安全路径故障处理。

## 8. BC/RL 集成到 SimulationEngine

### 8.1 配置控制模式

`configs/control.yaml` 默认使用 heuristic：

```yaml
default_mode: heuristic
per_uav: {}
```

将指定 UAV 切换到 BC/RL 时，必须显式配置：

```yaml
default_mode: heuristic
per_uav:
  UAV-1: bc
  UAV-2: rl
```

模式必须是 `heuristic`、`bc` 或 `rl`。显式配置了 BC/RL 但没有 provider 时，
启动会由 `ControlFactory.create_learning()` 快速失败；不会静默降级到启发式，也不
会伪造动作。

### 8.2 编写 provider

`SimulationEngine` 的 `control_providers` 参数接收一个按 `ControlMode` 索引的
映射。provider 可以接受 `(uav_id, task)` 两个参数，也可以只接受 `uav_id`；学习
模式创建时 task 为 `None`。

```python
from src.control import BCControllerBase, RLControllerBase
from src.control.common import ActionSpec, ControlMode, ObservationSpec
from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader


config = ConfigLoader.load()
observation_spec = ObservationSpec(
    config.control.observation.schema_version,
    config.control.observation.local_window_cells,
)

# Use the same action bounds as the runtime.  In production, centralize this
# calculation instead of duplicating constants in each provider.
nominal_speed = (
    config.uav.cruise_speed_kmh
    / config.grid.cell_size_km
    / 60.0
)
min_speed = nominal_speed * config.control.safety.min_speed_fraction
max_speed = nominal_speed * config.control.safety.max_speed_fraction
action_spec = ActionSpec(-max_speed, max_speed, min_speed, max_speed)


def make_bc(uav_id: str):
    controller = MyBCController(observation_spec, action_spec)
    controller.load_policy(PolicySource(f"models/{uav_id}/bc"))
    return controller


engine = SimulationEngine(
    config,
    control_providers={ControlMode.BC: make_bc},
)
```

上面示例中的 `PolicySource` 需要导入：

```python
from src.control.common import PolicySource
```

运行时会为配置为 BC 的 UAV 在构造阶段调用 provider 并启动 sortie。BC/RL UAV 不
会被普通 scheduler 任务分配覆盖；调度器仍然可以发布任务和目标事件，这些内容只
会作为下一次 `ControlObservation.events` 提供给 learning controller。

RL provider 的注册方式完全相同：

```python
engine = SimulationEngine(
    config,
    control_providers={ControlMode.RL: make_rl},
)
```

同一个 mode 只能注册一次；provider 返回值必须是 `ControllerBase`，且其
`control_mode` 必须与注册 mode 一致。provider 失败、schema 不匹配或未注册时应让
启动失败，便于尽早发现配置错误。

### 8.3 真实 LongCat-2.0 依赖

控制策略 provider 与任务调度器的 LongCat API 是两条边界，但创建
`SimulationEngine` 时仍会初始化 scheduler，并调用真实 LLM client 的
`assert_ready()`。真实 API 配置位于：

- `configs/llm_params.yaml`：provider、模型和 API base；当前模型为 `LongCat-2.0`。
- `configs/.env`：`LONGCAT_API_KEY`，该文件被 git 忽略。

`src/schedule/llm_client.py` 通过 OpenAI-compatible ChatCompletions API 发起真实
请求；未配置 key、请求失败或响应校验失败时不会伪造模型输出。启动前可以使用：

```bash
set -a
. configs/.env
set +a
python main.py --llm-probe-timeout 20
```

不要把 `.env` 内容、API key、完整 Authorization header 或包含密钥的调试对象写入
日志、测试输出或提交。

## 9. 运行时生命周期和错误处理

一次完整 tick 的顺序固定为：

1. 取出当前 UAV 已到期的事件；已消费事件从队列中删除。
2. 对 heuristic lease 按事件类型做一次性任务流转；learning lease 保留事件供
   controller 观察。
3. 从 `ObservationProvider` 创建不可变 observation。
4. 如果有待启动的 heuristic task，调用 `reset()` 和 `start_task()`。
5. 调用 `controller.act()` 获取 `ControlDecision`。
6. 检查 lease generation，拒绝过期 controller 的结果。
7. `SafetyEnvelope.apply()` 校验 schema、mask、有限值、速度/转弯界限、障碍和 SAR
   约束。
8. `UAVDynamicsExecutor.execute()` 应用唯一的动力学动作。
9. `OperationRegistry.reconcile()` 登记已应用的 TRACK、目标和传感器意图。
10. 将 controller event request 转为排队事件，供后续 tick 消费。

常见错误及预期行为：

| 错误 | 结果 |
| --- | --- |
| 未注册 BC/RL provider | `ControlFactoryError`，启动失败 |
| provider 返回错误模式 controller | `ControlFactoryError` 或 coordinator 校验错误 |
| observation schema 不匹配 | coordinator 拒绝 controller |
| 非有限或非法 action | `InvalidControlCommand`；连续达到阈值时触发紧急回收 |
| TRACK 的 contact 不在 action mask | `InvalidOperationIntent`，在状态变更前拒绝 |
| lease 已被替换 | `StaleControlCommand`，不执行过期命令 |
| 返航路线失效或没有安全路径 | 转 SYSTEM 故障处理；不能继续沿未验证路线飞行 |

控制器异常由仿真层转为安全返航流程。控制器不要捕获异常后偷偷输出一个默认动作，
因为这会掩盖模型或契约故障，并绕过明确的 SYSTEM recovery 机制。

## 10. 自定义控制器检查清单

提交自定义子类前，逐项确认：

- [ ] 从正确的 `src.control.*.base` 继承，没有复制一套相似接口。
- [ ] `control_mode`、`observation_spec`、`action_spec` 与注册模式和配置一致。
- [ ] 只读取 `ControlObservation`，没有保存或访问 `SimulationEngine`、`UAVEntity`、
      `Ship`、真值位置或 `StateManager` 引用。
- [ ] 动作是 `ControlCommand`，包含物理单位、合法 sensor/mode 和必要的 contact ID。
- [ ] 使用 `action_mask` 判断当前可选操作，不用隐藏真值推断目标。
- [ ] 不直接修改实体、调度器、信息场、事件队列或跟踪区域。
- [ ] BC 的 `decode_action()` 或 RL 的 `predict_action()` 返回正确的物理单位。
- [ ] RL 在 `reset_episode()` 清理 recurrent state；`predict_action()` 返回 next state。
- [ ] heuristic 在 `start_task()` 初始化任务，在 `stop_task()` 幂等清理，并且事件只发一次。
- [ ] provider 在构造 controller 时加载真实策略；没有 fake、mock 或静默 heuristic fallback。
- [ ] 覆盖正常动作、安全修正、非法动作、事件单次消费、lease 替换和返航故障测试。
- [ ] 使用真实配置和 `configs/.env` 做一次 LongCat 探活；验证输出中不包含密钥。

## 11. 推荐测试层级

```text
自定义 controller 单元测试
        |
contracts / base classes / safety
        |
factory provider 注册和 mode 校验
        |
coordinator 的 observe-act-safety-execute 顺序
        |
SimulationEngine 的配置、事件、返航和可视化集成
        |
真实 LongCat-2.0 探活与短仿真 smoke test
```

最小单元测试应验证：

```python
decision = controller.act(observation)
assert isinstance(decision.command, ControlCommand)
assert decision.command.schema_version == "control-command/v1"
```

集成测试应通过 `ControlFactory.register(ControlMode.BC/RL, provider)` 和
`SimulationEngine(control_providers=...)` 验证实际生命周期，不应直接调用私有字段
替代 coordinator。
