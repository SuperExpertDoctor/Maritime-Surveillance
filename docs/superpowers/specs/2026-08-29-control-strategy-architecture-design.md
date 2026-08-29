# UAV 底层控制策略架构设计

> 日期：2026-08-29
> 状态：已确认，待实施计划审阅
> 范围：`src/control` 重构及其在 `src/env`、`src/schedule`、配置、测试和可视化中的适配

## 1. 背景与问题

系统需要支持三种底层控制实现入口：

1. `heuristic`：区域覆盖和目标跟踪分别由启发式算法执行，通过事件机制完成任务流转；从 UAV 当前位置到任务区域使用 A* 导航。
2. `bc`：行为克隆控制的抽象基类。本轮不提供具体模型实现。
3. `rl`：强化学习控制的抽象基类。本轮不提供具体策略实现。

三种入口必须共享稳定的观测、动作、安全和控制权协议。启发式模式只在当前任务执行期间拥有 UAV 控制权；BC/RL 模式从 UAV 开始工作到工作里程耗尽期间持续拥有本机控制权，包括区域覆盖、目标跟踪及两者之间的切换。

当前实现与目标架构存在以下差距：

- `src/control` 只有 `waypoint.py`、`scan_pattern.py`、`track_orbit.py` 和 `return_path.py` 四个函数式模块，未形成控制器接口，也没有被主仿真循环使用。
- 实际覆盖规划位于 `src/utils/coverage_planner.py`，实际跟踪控制位于 `src/utils/track_orbit.py`，避障路径规划位于 `src/utils/obstacle_avoider.py`。
- `src/env/uav_entity.py` 同时承担实体状态、路径跟随、任务状态切换、传感器切换、跟踪控制和燃油判断，职责过多。
- `src/env/simulation.py` 直接调用 `assign_mission()`、`start_tracking()`、`cancel_tracking()`、`plan_return()` 等方法，直接决定搜索、跟踪、返航和任务恢复，无法保证学习控制器持续拥有控制权。
- 当前跨区导航使用 RRT* + Dubins，而不是本需求指定的 A*。
- `status`、`_mission_kind` 和 `sensor_mode` 被同时用于描述任务、执行阶段和控制权，无法可靠判断当前动作由谁产生。

## 2. 目标与非目标

### 2.1 目标

- 将 `src/control` 按 `heuristic`、`bc`、`rl` 和 `common` 四个功能目录组织。
- 三个实现目录分别提供 `base.py`；现有规则算法整合为 `HeuristicControllerBase` 的实现。
- 定义稳定、可版本化的观测和动作契约，BC/RL 子类不得直接依赖 `SimulationEngine` 或环境真值对象。
- 显式管理每架 UAV 的控制权，并防止调度器、事件处理器和过期控制器覆盖当前控制器动作。
- 在启发式任务转场中引入 A*，保留固定翼 Dubins 可飞性约束。
- 将安全约束与任务决策分离。安全层可以修正非法动作，但不能替控制器选择任务或目标。
- 以渐进方式迁移现有仿真，保持传感器、信息场、调度、可视化和回放接口可用。

### 2.2 非目标

- 本轮不实现具体 `BCController`、`RLController`、神经网络、权重格式或训练流水线。
- 本轮不实现 Gymnasium 环境；RL 基类只保留未来适配所需的空间和 episode 接口。
- 本轮不改变 LLM 区域划分、Hungarian 配对、AIS 判别、舰船运动或信息场数学模型。
- 本轮不把整个 `UAVEntity` 重写为新的物理引擎。
- 本轮不允许 BC/RL 读取未被传感器发现的舰船位置、真实军民属性等仿真真值。

## 3. 总体决策

采用“统一控制运行时 + 三类实现基类”的架构：

```text
Environment / Sensors / Scheduler
                |
                v
       ObservationProvider
                |
                v
       ControlCoordinator -------- ControlOwnership
          |                         SYSTEM / HEURISTIC / LEARNING
          |
          +-- heuristic/            单任务控制，事件驱动切换
          +-- bc/base.py            端到端控制抽象
          +-- rl/base.py            端到端控制抽象
                |
                v
          ControlCommand
                |
                v
          SafetyEnvelope
                |
                v
       UAVDynamicsExecutor
                |
                v
            UAVEntity
```

该方案拒绝两种替代设计：

- 只给现有 `SimulationEngine` 方法套薄接口：学习控制器仍会被现有状态机抢占控制权。
- 全量重写实体、调度器和物理引擎：边界最干净，但超出本次范围，回归风险过高。

## 4. 目录结构

```text
src/control/
  __init__.py

  heuristic/
    __init__.py
    base.py              # HeuristicControllerBase
    navigation.py        # Hybrid A* 转场，整合 waypoint.py
    coverage.py          # CoverageController，整合 scan_pattern.py
    tracking.py          # TrackingController，整合 track_orbit.py/LGVF
    return_to_base.py    # ReturnToBaseController/SystemHoldingController
    task_flow.py         # 基于事件的任务实例切换

  bc/
    __init__.py
    base.py              # BCControllerBase，仅抽象定义

  rl/
    __init__.py
    base.py              # RLControllerBase，仅抽象定义

  common/
    __init__.py
    base.py              # ControllerBase、LearningControllerBase
    contracts.py         # 观测、动作、事件、任务和枚举
    observation.py       # 统一观测读取与归一化
    safety.py            # SafetyEnvelope
    executor.py          # UAVDynamicsExecutor
    ownership.py         # 控制权和 generation
    coordinator.py       # observe-act-apply 主循环
    factory.py           # 按配置创建控制器
    operation_registry.py # 动作意图与跟踪区/传感器绑定登记
```

`src/control` 根目录不再放具体算法。原四个模块在一个兼容周期内转发到新包并发出 `DeprecationWarning`，待调用方迁移完成后删除。

原 `waypoint.py` 的导航不是独立任务，因此不会人为创建一个拥有独立控制权的导航控制器。它被整合为 `CoverageController`、`TrackingController` 和系统返航控制器的 A* 阶段；这三个可执行控制器均继承 `HeuristicControllerBase`。`navigation.py` 只保存无状态路径规划辅助类。

## 5. 核心数据契约

所有契约定义在 `src/control/common/contracts.py`。契约使用 dataclass、Enum 和不可变集合，不携带 `SimulationEngine`、`UAVEntity`、`Ship` 或 `StateManager` 的对象引用。

### 5.1 枚举

```python
class ControlMode(str, Enum):
    HEURISTIC = "heuristic"
    BC = "bc"
    RL = "rl"


class ControlOwner(str, Enum):
    SYSTEM = "system"
    HEURISTIC = "heuristic"
    LEARNING = "learning"


class OperationMode(str, Enum):
    IDLE = "idle"
    TRANSIT = "transit"
    COVERAGE = "coverage"
    TRACK = "track"
    RETURN = "return"
    HOLDING = "holding"


class SensorMode(str, Enum):
    OFF = "off"
    SAR = "sar"
    EO = "eo"
```

控制模式、控制权和作业模式必须分开。`ControlMode.RL` 不等于当前正在 `TRACK`；`OperationMode.TRACK` 也不能推导控制权属于谁。

`control_mode` 是每架 UAV 的配置属性，在系统返航、等待和加油阶段仍保持原值；系统命令源即使复用启发式基类，也不得把 BC/RL UAV 的配置模式改写为 heuristic。

### 5.2 观测

```python
@dataclass(frozen=True)
class UAVObservation:
    uav_id: str
    position: tuple[float, float]
    heading_rad: float
    speed_cells_min: float
    remaining_range_cells: float
    control_mode: ControlMode
    control_owner: ControlOwner
    operation_mode: OperationMode
    sensor_mode: SensorMode
    safety_intervened: bool


@dataclass(frozen=True)
class ContactObservation:
    contact_id: str
    group_id: str | None
    estimated_position: tuple[float, float]
    estimated_velocity: tuple[float, float]
    source: str
    observed_at_min: float
    age_min: float
    confidence: float


@dataclass(frozen=True)
class HazardObservation:
    hazard_id: str
    hazard_type: str
    center: tuple[float, float]
    half_extent_cells: float
    velocity_cells_min: tuple[float, float]
    intensity: float


@dataclass(frozen=True)
class BaseObservation:
    base_id: str
    position: tuple[float, float]
    capacity: int
    reserved_load: int


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

第一版 `schema_version` 固定为 `control-observation/v1`。数组 shape 由地图分辨率和固定 local window 配置决定，dtype 固定为 `float32` 或 `bool`，构建后设为只读。缺失目标用空 tuple 和 mask 表示，不用变长的真值对象替代。

`ActionMask` 由 ObservationProvider 根据当前 owner/mode 和可观测资源生成：HEURISTIC/LEARNING 作业阶段允许 TRANSIT/COVERAGE，存在有效 contact 时才允许 EO/TRACK；SYSTEM 返航阶段只允许 RETURN/HOLDING。OFF 始终可用，`target_contact_ids` 只列出本帧 contact ID。SAR 的直线稳定性在安全层结合本步转弯率校验；mask 不替控制器决定当前任务。

`planning_obstacle_mask` 是只读的全局规划快照，只包含已发布的陆地和障碍物占用，不包含舰船真值。局部窗口供策略输入，全局规划快照供 A* 使用；控制器不得绕过这两个快照访问环境对象。

### 5.3 动作

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
class ControllerEventRequest:
    event_type: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class ControlDecision:
    command: ControlCommand
    events: tuple[ControllerEventRequest, ...] = ()
```

动作表达单个仿真步的本机控制，不表达整条航路。`ControlDecision` 为命令附带控制器产生的事件请求；协调器负责分配事件 sequence，并把事件放入下一 tick 队列。启发式控制器可以内部维护航路，但必须通过相同的 `ControlCommand` 驱动实体。BC/RL 后续实现也必须输出该物理单位动作；归一化模型输出由各自子类解码。

### 5.4 任务和事件

```python
@dataclass(frozen=True)
class RecoveryPlan:
    base_id: str
    base_position: tuple[float, float]
    reservation_id: str
    path: tuple[tuple[float, float, float], ...]
    path_length_cells: float
    reserve_cells: float
    planning_map_version: int


@dataclass(frozen=True)
class ControlTask:
    task_id: str
    task_type: OperationMode
    region_bbox: BBox | None = None
    target_contact_id: str | None = None
    recovery_plan: RecoveryPlan | None = None


@dataclass(frozen=True)
class ControlEvent:
    sequence: int
    timestamp_min: float
    event_type: str
    source: str
    uav_id: str | None
    payload: Mapping[str, object]


@dataclass(frozen=True)
class ControllerContext:
    uav_id: str
    dt_min: float
    observation_spec: ObservationSpec
    action_spec: ActionSpec
    episode_id: str
    task: ControlTask | None = None
```

事件具有单调递增 sequence。协调器按 `(timestamp_min, sequence)` 顺序投递，每个控制器只消费一次。事件 payload 只能包含可序列化快照。

事件只有一条权威投递路径：外部事件先由 `HeuristicTaskFlow` 判断是否为任务转换事件；被消费的转换事件不会再放入新旧控制器的 observation。未被任务流消费的事件只通过下一帧 `ControlObservation.events` 交给当前控制器，不再调用第二套 `handle_event()` 回调。学习控制器的普通任务事件全部走 observation，任务流不得消费或替换其 lease。

## 6. 基类设计

### 6.1 公共基类

`src/control/common/base.py`：

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


class LearningControllerBase(ControllerBase):
    @property
    def ownership_scope(self) -> str:
        return "sortie"

    @abstractmethod
    def load_policy(self, source: PolicySource) -> None: ...
```

`ControllerBase` 明确包含观测和动作规格接口，满足启发式与学习实现都能读取控制决策所需观测的要求。

### 6.2 启发式基类

`src/control/heuristic/base.py`：

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

    # Transition events are consumed by HeuristicTaskFlow before act().
```

覆盖、跟踪和返航控制器继承该类。每个实例只负责一个明确任务；事件流组件负责创建和原子替换任务控制器。

### 6.3 BC 基类

`src/control/bc/base.py` 只声明模板方法，不绑定 PyTorch、ONNX 或其他框架：

```python
class BCControllerBase(LearningControllerBase):
    @property
    def control_mode(self) -> ControlMode:
        return ControlMode.BC

    @abstractmethod
    def encode_observation(self, observation: ControlObservation) -> object: ...

    @abstractmethod
    def predict_action(self, encoded_observation: object) -> object: ...

    @abstractmethod
    def decode_action(self, model_output: object) -> ControlCommand: ...
```

基类的 `act()` 实现固定执行 `encode_observation -> predict_action -> decode_action`，具体子类只实现框架和模型细节。

### 6.4 RL 基类

`src/control/rl/base.py` 同样不绑定具体 RL 库：

```python
class RLControllerBase(LearningControllerBase):
    @property
    def control_mode(self) -> ControlMode:
        return ControlMode.RL

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

`ControllerBase.reset()` 保存 context，不再是无实现的抽象方法。`BCControllerBase.act()` 将解码后的命令包装为无事件的 `ControlDecision`。`RLControllerBase` 保存 `_policy_state` 和 `_deterministic`；其 `act()` 调用 `predict_action()`、更新隐状态并返回无事件的 `ControlDecision`，`set_evaluation_mode()` 决定 deterministic 参数。episode ID 由协调器在 `start_work()` 时生成并放入 `ControllerContext`。本轮只验证这些抽象类不能直接实例化、最小测试子类可以实例化并遵循公共契约。

## 7. 启发式控制实现

### 7.1 Hybrid A* 转场

`heuristic/navigation.py` 提供 `AStarNavigator`，以曲率受限的 Hybrid A* 替代 `control/waypoint.py` 的直线路径。Hybrid A* 仍使用 A* 的开放集、累计代价和启发函数，但在连续 pose 上展开固定翼可飞运动原语：

- 输入：当前 pose、目标 `BBox` 或目标接近圈、观测中的全局只读规划 mask、最小转弯半径。
- 节点保存连续 `(x, y, heading)`；closed-set key 按 `astar_xy_resolution_cells` 和航向 bin 离散，默认 0.5 cell、72 个航向 bin。
- 后继动作使用曲率 `{-1/r_min, 0, +1/r_min}`，沿直线或圆弧积分；原语长度至少覆盖一个航向 bin，公式为 `max(configured_length, 2*pi*r_min/heading_bins)`，因此任意有限 `r_min` 都存在左/直/右后继。
- 每条原语以 `path_sample_step_cells` 采样，逐点验证边界、障碍和对角相邻 cell，禁止穿越被占用 cell 或切角。
- 启发函数使用忽略障碍的 2-D 距离下界；航向只作为稳定 tie-breaker，不破坏 admissibility。
- 目标：到区域边界的可飞入 cell，而不是固定 bbox 中心；从候选目标中选择总代价最低者。
- 动态雷云变化使剩余路径无效时产生 `route_blocked`，由当前启发式控制器重新规划。
- `BBox` 使用现有半开区间语义；目标边界 cell 是 `[col_start, col_end-1] x [row_start, row_end-1]` 的内侧周界。A* 节点 `(col,row)` 与 `GridCoord` 相同，转换 pose 时使用 `(float(col), float(row), heading)`。
- 搜索产生的运动原语本身已经满足最小转弯半径。Dubins 只作为从已展开节点到候选目标 pose 的解析入场原语；入场曲线不安全时仅标记该 `(node key, goal pose)` 尝试失败，继续展开开放集，不把跨越多段的曲线错误映射为某一条网格边。
- 按总代价依次尝试目标解析入场，最多尝试配置的 candidate limit；未命中时继续 Hybrid A* 搜索，开放集穷尽后才报告无路径。
- 没有可行路径时抛出 `PathNotFoundError`，转换为 `task_failed` 事件，不允许退回直线穿障路径。

Hybrid A* 负责“去哪里走”并在搜索阶段保证固定翼可转；Dubins 只加速最终入场。该组合仍然是 A* 主导的区域转场。

### 7.2 区域覆盖

`CoverageController` 的内部阶段为：

```text
CREATED -> TRANSIT_ASTAR -> ALIGN_SCAN -> SCANNING -> COMPLETED
```

- `TRANSIT_ASTAR` 使用 `AStarNavigator` 到达首条扫描带入口。
- `ALIGN_SCAN` 关闭 SAR，直到航向误差进入现有 `sar_heading_tolerance_rad`。
- `SCANNING` 复用 `CoveragePlanner` 的扫描带和 Dubins 转弯，并逐步输出控制命令。
- 只有位于稳定直线扫描段时输出 `SensorMode.SAR`。
- 完成后在 `ControlDecision.events` 中请求产生 `search_complete`，自身不选择下一个任务。

### 7.3 目标跟踪

`TrackingController` 的内部阶段为：

```text
CREATED -> APPROACH_ASTAR -> ORBIT_ENTRY -> TRACKING -> COMPLETED/LOST
```

- 接近阶段以目标传感器估计位置为中心，A* 规划到 EO 可探测距离内的安全接近点。
- 进入阶段复用 `LGVFTracker.plan_entry()`。
- 跟踪阶段复用 `LGVFTracker.compute_guidance()` 和现有雷云规避逻辑。
- 所有目标更新来自 `ContactObservation`，不读取 `_group_center()` 的真值。
- 目标丢失、民船释放、目标驶离等外部事件由 `task_flow.py` 在决策前消费；控制器内部的路径失败通过 `ControlDecision.events` 请求产生 `task_failed`。

### 7.4 返航与系统等待

`ReturnToBaseController` 继承启发式基类以复用同一动作管线，但其运行时 owner 保持 `SYSTEM`：

- 系统在工作里程耗尽后回收控制权。
- 回收前，系统对每个有容量基地生成并验证 A* + Dubins 路径，以实际路径长度加安全余量判断可达性。
- 系统按“实际安全路径长度、基地 ID”排序并原子预留基地容量，形成不可变 `RecoveryPlan`；只有 plan 和 reservation 同时成功后才回收控制权。
- 返航控制器的 `start_task()` 只接受带 `RecoveryPlan` 的 RETURN 任务，`act()` 跟随已验证路径，`is_complete()` 判断到达基地，`stop_task()` 释放未使用 reservation。
- 返航控制器记录 `planning_map_version`。版本变化时先校验剩余路径；若失效，则在保留原基地 reservation 的前提下用最新快照重新规划到同一基地，并再次校验 `new_path_length + reserve <= remaining_range`。
- 返航重规划无路或剩余里程不足时抛出 `NoSafeRecoveryPath`，系统进入明确 emergency failure，不改投未预留基地，也不执行旧路径或直线替代。
- `SystemHoldingController` 同样继承启发式基类，但 lease owner 为 SYSTEM；它在启发式任务结束而新任务尚未分配时输出 `HOLDING/OFF`，沿经过安全层校验的固定翼等待圈飞行，避免出现无命令空帧。
- 返航、等待和加油不属于启发式任务模式或学习策略的作业控制区间。
- 如果没有任何安全可达基地，系统不先回收再猜测路线；它产生 `no_safe_recovery_path` 并进入显式 emergency failure，禁止执行未验证的返航路径。

### 7.5 任务流

`HeuristicTaskFlow` 订阅任务事件并返回 `TaskTransition`：

- `target_found`：覆盖控制器停止，创建跟踪控制器。
- `target_lost`、`civilian_released`、`target_departed`：跟踪控制器停止，优先恢复原搜索区域，否则请求调度器分配。
- `search_complete`：如果已有原子确定的下一任务则直接替换控制器；否则释放到 SYSTEM/HOLDING，并安装系统等待控制器，等待调度器下发下一任务。
- `route_blocked`：覆盖/跟踪控制器内部重规划；达到重试上限后产生 `task_failed`。SYSTEM 返航由返航控制器按 7.4 节的保留 reservation 规则重规划。
- `work_range_exhausted` 是生命周期事件，不由任务流消费；系统完成 RecoveryPlan 预验证和基地预留后，通过协调器回收控制权。

控制器替换必须在同一仿真 tick 的决策前完成。任务流先构造并校验 replacement controller/task，再在协调器临界区内一次性替换 lease、controller registry 和 pending task；旧控制器随后只做幂等资源清理，不再产生动作。协调器重新读取新 lease 和新 controller，基于新 lease 构建观测，对待启动任务执行 `reset(context)` 和 `start_task(task, observation)`，再只调用新 controller 一次。旧 generation 命令失效，不能出现双 owner 或无人控制的空帧。

## 8. 控制权模型

`ControlOwnership` 至少包含：

```python
@dataclass(frozen=True)
class ControlLease:
    uav_id: str
    owner: ControlOwner
    controller_id: str
    generation: int
    acquired_at_min: float
```

正常状态转换：

```text
SYSTEM --work_started(heuristic)--> HEURISTIC
SYSTEM --work_started(bc/rl)-----> LEARNING
HEURISTIC --有后继任务-----------> HEURISTIC（generation + 1）
HEURISTIC --无后继任务-----------> SYSTEM / HOLDING
HEURISTIC --work_range_exhausted-> SYSTEM
LEARNING --工作中的任意任务事件--> LEARNING（generation 不变）
LEARNING --work_range_exhausted--> SYSTEM
```

“工作里程耗尽”定义为：

```text
remaining_range_cells
    <= validated_return_path_cells
       + configured_reserve_cells
       + max_command_distance_next_tick
```

其中返航距离必须来自当前全局规划快照上的 A* + Dubins 完整可飞路径，`max_command_distance_next_tick = max_speed_cells_min * dt_min` 用于消除离散步进晚触发的风险。它表示可用于任务的里程已耗尽，不是物理燃油为零。系统必须保留合法返航所需里程。

异常状态 `EMERGENCY_REVOKE` 只用于控制器异常、连续非法动作或无法保证飞行安全。它不作为正常任务切换方式，并产生可审计事件。

命令应用必须同时校验 `uav_id`、`controller_id` 和 `generation`。任何旧 lease 产生的延迟命令均丢弃。

## 9. 观测边界

`ObservationProvider` 是控制器读取世界状态的唯一入口：

- 本机状态来自 `UAVEntity` 的公开物理属性。
- 地图信息来自 `StateManager` 的信息场、可搜索 mask 和障碍 mask。
- 目标只来自 SAR/EO/AIS 产生的 `TargetReport` 或等价传感器报告。
- 雷云和岛屿以只读 `HazardObservation` 发布；控制器不能持有可变障碍物实体。
- 共享 UAV 状态来自 `UAVState`，不包含其他控制器内部状态。
- 启发式任务可额外在 `ControllerContext` 中获得已分配 `ControlTask`；BC/RL 不接收强制任务分配。
- `Ship.float_position`、`actual_military`、未发现舰船列表、未来雷云轨迹和随机数生成器均禁止进入观测。

测试通过构造带有明显哨兵值的隐藏舰船，递归检查观测内容，防止后续字段扩展造成真值泄漏。

## 10. 安全与动作执行

### 10.1 SafetyEnvelope

安全层按以下顺序处理命令：

1. 拒绝 NaN、inf、缺失字段和非 `control-command/v1` schema。
2. 将速度裁剪到机型允许范围。
3. 将转弯率裁剪到固定翼限制。
4. 预测下一步轨迹，防止越界、进入大陆或雷云硬安全区。
5. 校验传感器动作，例如非稳定直线段禁止 SAR 成像。

输出 `SafetyResult(requested_command, applied_command, interventions)`。请求不存在的 contact 或被 mask 禁止的 operation mode 属于非法动作；安全层不猜测替代目标，也不把 COVERAGE 改为 TRACK。SAR 请求在非 COVERAGE 模式或本步转弯率超过稳定阈值时可被关闭，并记录传感器安全干预。

### 10.2 UAVDynamicsExecutor

执行器是修改 UAV 位置、航向、速度和传感器模式的唯一控制入口。它负责：

- 按 `turn_rate_rad_min` 和 `speed_cells_min` 积分连续 pose。
- 累加实际飞行距离并扣减剩余里程。
- 更新 `operation_mode` 和兼容的旧 `status` 字段。
- 保存 `requested_command`、`applied_command` 和 safety intervention，供诊断与未来数据采集使用。

实体仍可保留传感器对象和可视化轨迹，但不再在 `_update_route_state()` 中自行决定任务切换。

## 11. 每步数据流

新的 `SimulationEngine.step()` 顺序固定为：

```text
1. 时钟推进
2. 障碍物和舰船物理更新
3. 对 heuristic UAV 先用任务流消费上一步转换事件并原子替换 controller/lease
4. 重新读取当前 controller/lease，构建带 current_time 的 ControlObservation
5. ControlCoordinator 只调用当前 controller 一次，得到 ControlDecision
6. 再次校验 act() 所用 lease 未变化
7. SafetyEnvelope 生成 applied_command
8. 更新连续 invalid 计数；达到阈值时在执行前中止该命令
9. UAVDynamicsExecutor 执行动作和里程消耗
10. OperationRegistry 按 applied command 登记/释放跟踪占用与传感器绑定
11. 更新 SAR/EO/AIS 传感器和信息场
12. 为控制器事件请求分配 sequence，并与本步环境事件一起排入下一 tick
13. TaskAllocator 仅处理 owner 为 SYSTEM、operation mode 为 IDLE/HOLDING 且 control mode 为 heuristic 的 UAV
14. 同步 StateManager、冲突检测和可视化快照
```

本步新传感器事件在下一控制 tick 进入观测，形成明确的一步延迟，避免同一 tick 内因遍历顺序导致不同 UAV 看到不同世界状态。

## 12. 与调度和事件机制的适配

- `TaskAllocator` 增加 eligible predicate，只将 `control_mode == heuristic`、`owner` 可接受任务且状态空闲的 UAV 放入 Hungarian 配对。
- `TriggerManager` 仍可为全局区域规划生成 heavy/light 决策，但这些决策不能直接改变 BC/RL UAV 的动作或 operation mode。
- `StateManager` 保存控制模式和控制权快照，供调度、可视化和回放使用。
- 启发式目标发现事件交给 `HeuristicTaskFlow`；学习模式目标发现事件只进入下一帧观测。
- 学习控制器输出 `TRACK + target_contact_id` 时，环境可以创建/更新跟踪区和传感器绑定，这是世界状态登记，不是外部路径规划。
- 学习控制器输出离开 TRACK 时，环境释放其跟踪占用，但不得自动为它恢复搜索航路。

上述两条由 `common/operation_registry.py` 统一实现。Registry 只接受 applied command 和本帧 contact snapshot：TRACK 必须引用有效 contact；进入 TRACK 时创建或绑定跟踪区，持续 TRACK 时更新登记，离开 TRACK 时释放本 UAV 的跟踪占用。该组件不产生航向、速度或后继任务。

## 13. 配置

新增 `configs/control.yaml`：

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

`per_uav` 为未来混合评估保留，例如 `UAV-1: bc`。如果选择 `bc` 或 `rl` 而没有注册具体子类，`ControlFactory` 必须在引擎启动时抛出清晰错误，不能静默退回 heuristic。

`SimulationEngine` 提供可选的 `control_providers`（或预配置 `ControlFactory`）构造参数。外部调用方必须能在模式校验前注册 BC/RL provider；默认不传时保持现有构造 API 和 heuristic 启动行为。本轮只定义该插件入口和抽象基类，不提供生产 BC/RL provider。

## 14. 兼容迁移

迁移按以下阶段进行，每个阶段均可独立测试：

1. 建立契约、基类、配置和控制权组件，不改变现有仿真路径。
2. 建立观测、安全和执行器，并用特征测试锁定现有动力学、SAR 和燃油行为。
3. 将现有 `src/control` 函数整合到 heuristic 控制器，加入 A*，保留旧导入包装。
4. 接入 `ControlCoordinator`，先只启用 heuristic 模式。
5. 将搜索/跟踪切换迁入 `HeuristicTaskFlow`，移除仿真引擎对任务控制方法的直接调用。
6. 增加 BC/RL 抽象契约测试和 fail-fast 工厂行为。
7. 更新状态、可视化和文档，运行全量回归。

在兼容期内，`UAVEntity.assign_mission()` 等旧方法保留签名并发出弃用提示，供外部调用方迁移；生产仿真路径和新代码禁止再调用其任务切换分支，后续版本统一删除，避免实体反向依赖协调器。

## 15. 错误处理

- A* 无路径：产生 `task_failed`，启发式任务流释放任务并请求重分配。
- 动态障碍阻断：最多按配置重规划三次；每次记录原因和路径版本。
- 非法动作：schema/mask/数值拒绝和任何成功安全修正都记为一次 invalid；只有完全无干预的动作才把连续计数清零，连续三次 invalid 触发 `EMERGENCY_REVOKE`。
- 控制器异常：训练/测试环境直接抛出；运行环境中，仅 HEURISTIC/LEARNING 作业控制器异常会在完成安全返航路径验证和基地预留后回收为系统返航。SYSTEM/RETURN 的 `NoSafeRecoveryPath` 优先直接进入 emergency failure，不再尝试其他基地；SYSTEM/HOLDING 等系统控制器异常同样直接失败，避免递归恢复。
- 未注册 BC/RL：引擎初始化失败，错误包含 UAV ID、模式和缺失注册项。
- 观测 schema 不匹配：控制器启动失败，不在运行中尝试猜测或转换。

## 16. 测试策略

新增 `tests/control/`：

- `test_contracts.py`：枚举、dataclass、schema 和动作规格。
- `test_base_classes.py`：三类基类抽象性和最小子类契约。
- `test_observation.py`：shape/dtype、只读数组、事件顺序和真值隔离。
- `test_ownership.py`：lease、generation、正常回收和异常回收。
- `test_safety.py`：数值裁剪、边界、障碍、SAR 约束和审计结果。
- `test_executor.py`：动力学积分、里程消耗和兼容状态映射。
- `heuristic/test_navigation.py`：A* 最短性、不可达、禁止切角、动态重规划和 Dubins 安全性。
- `heuristic/test_coverage.py`：A* 转场、扫描阶段、SAR 开关和完成事件。
- `heuristic/test_tracking.py`：A* 接近、LGVF、目标报告更新和丢失事件。
- `heuristic/test_task_flow.py`：覆盖/跟踪原子切换和旧命令失效。
- `test_coordinator.py`：每 UAV 每 tick 一次决策、事件延迟和 owner 路由。
- `test_factory.py`：heuristic 创建成功，未注册 BC/RL fail-fast。
- `test_operation_registry.py`：学习/启发式 TRACK 意图的登记、持续、释放和非法 contact。
- `test_simulation_ownership.py`：仿真级控制权、同帧切换和工作里程回收。

现有以下测试需要迁移而不是删除断言：

- `tests/env/test_simulation_integration.py`
- `tests/env/test_storm_avoidance.py`
- `tests/utils/test_coverage_planner.py`
- `tests/utils/test_track_orbit.py`
- `tests/schedule/test_task_allocator.py`
- `tests/schedule/test_state_manager.py`
- `tests/env/test_goal2_foundation.py`

## 17. 验收标准

1. `src/control/heuristic`、`src/control/bc`、`src/control/rl` 均有 `base.py`，其公共观测和动作接口类型一致。
2. 当前 `src/control` 的四项功能均已整合到 heuristic 包并可经控制协调器运行。
3. 启发式覆盖和跟踪转场由 A* 主导，路径不穿越障碍且符合固定翼转弯约束。
4. 启发式模式任务切换不出现空 owner、双 owner 或旧控制器命令生效。
5. BC/RL 模式的抽象控制器拥有 sortie 级控制权语义；正常任务事件不能回收控制权。
6. 学习模式下，系统只在开始工作前、工作里程耗尽后、返航/等待/加油阶段持有正常控制权；启发式模式无活动任务时允许 SYSTEM/IDLE 或 SYSTEM/HOLDING 等待调度。
7. SafetyEnvelope 的每次动作修正均可追溯，且不改变任务选择。
8. 控制观测不包含环境隐藏真值。
9. 未提供具体 BC/RL 实现时，heuristic 默认配置可运行；显式选择 BC/RL 会清晰失败。
10. `python -m pytest -q` 全量通过，固定 seed 的仿真仍可复现。

## 18. 后续扩展

完成本轮后，可在不修改仿真主循环的前提下：

- 继承 `BCControllerBase` 接入具体 BC 模型和专家轨迹数据。
- 继承 `RLControllerBase` 接入具体策略库，再实现 `SingleUAVControlEnv`。
- 用共享参数创建每 UAV 独立策略实例，实现集中训练、分散执行。
- 将 requested/applied command 和 intervention 扩展为标准离线数据集。

这些扩展必须遵守本设计的观测真值隔离和 sortie 级控制权规则。
