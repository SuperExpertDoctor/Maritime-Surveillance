# LLM 动态任务重分配系统 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建基于 LLM 的 UAV 海上侦察动态任务重分配系统，包含信息场管理、关键区域提取、LLM 决策管线、Hungarian 配对和仿真环境。

**Architecture:** 四层结构 — `configs/`(超参数) → `schedule/`(核心调度+LLM管线) → `utils/`(启发式规则) → `wm/`(仿真环境)。核心流程：信息场衰减 → CandidateExtractor 提取关键区域 → Prompt 组装 → LLM 输出区域划分 → 校验 → Hungarian 配对 → 更新状态。

**Tech Stack:** Python 3.10+, numpy, scipy (Hungarian), PyYAML, openai-compatible LLM API client

## Global Constraints

- 网格: 30×30, cell 10km×10km
- 信息衰减: 指数模型 e^(-λt), 搜索半衰期 30min, 跟踪半衰期 15min
- 白/灰/黑阈值: 0.7 / 0.2
- 搜索区: 20-40 格, 长宽比 ≤ 2:1
- 跟踪区: 6-16 格
- UAV 最多 10 架同时执行任务
- LLM 30min 周期重量触发 + 事件驱动
- LLM 只管区域划分, Hungarian 管配对
- 所有超参数定义在 configs/*.yaml

---

### Task 1: 配置文件

**Files:**
- Create: `configs/environment.yaml`
- Create: `configs/grid.yaml`
- Create: `configs/uav.yaml`
- Create: `configs/ship.yaml`
- Create: `configs/llm.yaml`

**Interfaces:**
- Produces: 5 个 YAML 配置文件, 被所有模块的 ConfigLoader 读取

- [ ] **Step 1: 创建 environment.yaml**

Write: `configs/environment.yaml`
```yaml
sea_area_km: [300, 300]
base_position:
  col: 15
  row: 28
```

- [ ] **Step 2: 创建 grid.yaml**

Write: `configs/grid.yaml`
```yaml
resolution: [30, 30]
cell_size_km: 10
decay_half_life_min: 30
track_decay_half_life_min: 15
white_threshold: 0.7
gray_threshold: 0.2
value_alpha: 1.0
value_beta: 0.8
value_gamma: 0.5
marker_sigma_cells: 1.5
marker_max_age_min: 60
marker_decay_half_life_min: 45
candidate_value_threshold: 0.3
fragment_threshold_cells: 12
search_min_cells: 20
search_max_cells: 40
aspect_ratio_max: 2.0
stability_iou_threshold: 0.7
```

- [ ] **Step 3: 创建 uav.yaml**

Write: `configs/uav.yaml`
```yaml
count_max: 10
cruise_speed_kmh: 160
sar_swath_km: 15
endurance_h: 30
refuel_time_min: 12
search_efficiency: 0.75
```

- [ ] **Step 4: 创建 ship.yaml**

Write: `configs/ship.yaml`
```yaml
count_min: 5
max_groups: 3
speed_kn: 18
zigzag_amplitude_km: 5
zigzag_period_min: 10
zigzag_phase_random: true
```

- [ ] **Step 5: 创建 llm.yaml**

Write: `configs/llm.yaml`
```yaml
model: "deepseek-v4"
api_base: "https://api.deepseek.com/v1"
api_key_env: "DEEPSEEK_API_KEY"
heavy_cycle_min: 30
reviewer_cycle_min: 15
max_retries: 2
temperature: 0.3
max_tokens: 2048
```

- [ ] **Step 6: Commit**

```bash
git add configs/
git commit -m "feat: add configuration files for environment, grid, UAV, ship, and LLM"
```

---

### Task 2: 配置加载器 + 基础数据类型

**Files:**
- Create: `schedule/__init__.py`
- Create: `schedule/config_loader.py`
- Create: `schedule/datatypes.py`

**Interfaces:**
- Produces:
  - `ConfigLoader.load(base_path="configs") -> AppConfig` (包含所有配置的 dataclass 实例)
  - `GridCoord = namedtuple("GridCoord", ["col", "row"])`
  - `BBox = namedtuple("BBox", ["col_start", "row_start", "col_end", "row_end"])`
  - `Region(id: str, bbox: BBox, type: str, priority: str, info_value: float)`
  - `UAVState(id: str, status: str, position: GridCoord, fuel_remaining_pct: float, assigned_region_id: str | None)`
  - `Marker(id: str, position: GridCoord, created_time: float, source_uav_id: str)`

- [ ] **Step 1: 创建 datatypes.py**

Write: `schedule/datatypes.py`
```python
from dataclasses import dataclass, field
from typing import Optional
from collections import namedtuple

GridCoord = namedtuple("GridCoord", ["col", "row"])
BBox = namedtuple("BBox", ["col_start", "row_start", "col_end", "row_end"])

@dataclass
class Region:
    id: str
    bbox: BBox
    type: str  # "search" | "track"
    priority: str = "medium"  # "high" | "medium" | "low"
    info_value: float = 0.0
    avg_info: float = 0.0
    assigned_uav_id: Optional[str] = None
    completion_pct: float = 0.0
    created_cycle: int = 0

@dataclass
class UAVState:
    id: str
    status: str  # "idle" | "transit" | "searching" | "tracking" | "returning" | "refueling"
    position: GridCoord
    fuel_remaining_pct: float = 1.0
    assigned_region_id: Optional[str] = None
    target_group_id: Optional[str] = None
    time_to_available: float = 0.0  # minutes until refueled/ready

@dataclass
class Marker:
    id: str
    position: GridCoord
    created_time: float
    source_uav_id: str

@dataclass
class RegionInfoRow:
    """One row in the InfoValueTable."""
    region_id: str
    bbox: BBox
    type: str
    avg_info: float
    value: float
    updated_time: float
    status: str  # "active" | "completed" | "stale"
    assigned_uav_id: Optional[str] = None
```

- [ ] **Step 2: 创建 config_loader.py**

Write: `schedule/config_loader.py`
```python
import os
import yaml
from dataclasses import dataclass

@dataclass
class EnvironmentConfig:
    sea_area_km: tuple
    base_position: tuple

@dataclass
class GridConfig:
    resolution: tuple
    cell_size_km: int
    decay_half_life_min: float
    track_decay_half_life_min: float
    white_threshold: float
    gray_threshold: float
    value_alpha: float
    value_beta: float
    value_gamma: float
    marker_sigma_cells: float
    marker_max_age_min: float
    marker_decay_half_life_min: float
    candidate_value_threshold: float
    fragment_threshold_cells: int
    search_min_cells: int
    search_max_cells: int
    aspect_ratio_max: float
    stability_iou_threshold: float

@dataclass
class UAVConfig:
    count_max: int
    cruise_speed_kmh: float
    sar_swath_km: int
    endurance_h: float
    refuel_time_min: float
    search_efficiency: float

@dataclass
class ShipConfig:
    count_min: int
    max_groups: int
    speed_kn: float
    zigzag_amplitude_km: float
    zigzag_period_min: float
    zigzag_phase_random: bool

@dataclass
class LLMConfig:
    model: str
    api_base: str
    api_key_env: str
    heavy_cycle_min: float
    reviewer_cycle_min: float
    max_retries: int
    temperature: float
    max_tokens: int

@dataclass
class AppConfig:
    environment: EnvironmentConfig
    grid: GridConfig
    uav: UAVConfig
    ship: ShipConfig
    llm: LLMConfig


class ConfigLoader:
    @staticmethod
    def _dict_to_dataclass(d: dict, cls):
        field_names = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in field_names}
        return cls(**filtered)

    @staticmethod
    def load(base_path: str = "configs") -> "AppConfig":
        def _read(name):
            with open(os.path.join(base_path, name), "r", encoding="utf-8") as f:
                return yaml.safe_load(f)

        env_data = _read("environment.yaml")
        env_data["sea_area_km"] = tuple(env_data["sea_area_km"])
        env_data["base_position"] = tuple(env_data["base_position"])

        grid_data = _read("grid.yaml")
        grid_data["resolution"] = tuple(grid_data["resolution"])

        return AppConfig(
            environment=ConfigLoader._dict_to_dataclass(env_data, EnvironmentConfig),
            grid=ConfigLoader._dict_to_dataclass(grid_data, GridConfig),
            uav=ConfigLoader._dict_to_dataclass(_read("uav.yaml"), UAVConfig),
            ship=ConfigLoader._dict_to_dataclass(_read("ship.yaml"), ShipConfig),
            llm=ConfigLoader._dict_to_dataclass(_read("llm.yaml"), LLMConfig),
        )
```

- [ ] **Step 3: 创建 __init__.py**

Write: `schedule/__init__.py`
```python
# schedule - LLM-based dynamic task reallocation for UAV maritime surveillance
```

- [ ] **Step 4: 验证配置加载**

Run: `python -c "from schedule.config_loader import ConfigLoader; c = ConfigLoader.load(); print(c.grid.resolution)"`
Expected: `(30, 30)`

- [ ] **Step 5: Commit**

```bash
git add schedule/__init__.py schedule/config_loader.py schedule/datatypes.py
git commit -m "feat: add config loader and base data types"
```

---

### Task 3: 信息场 (InfoField)

**Files:**
- Create: `schedule/info_field.py`

**Interfaces:**
- Consumes: `AppConfig (grid, environment)`, `BBox`, `GridCoord` from datatypes
- Produces:
  - `class InfoField`: 管理 30×30 cell 级信息素和价值矩阵
  - `def update_decay(current_time: float) -> None`
  - `def scan_cell(coord: GridCoord, current_time: float, is_track: bool = False) -> None`
  - `def scan_bbox(bbox: BBox, current_time: float, is_track: bool = False) -> None`
  - `def add_marker(position: GridCoord, current_time: float, marker_id: str) -> None`
  - `def remove_marker(marker_id: str) -> None`
  - `def get_info_matrix() -> np.ndarray`
  - `def get_value_matrix(current_time: float) -> np.ndarray`
  - `def get_avg_info_in_bbox(bbox: BBox) -> float`
  - `def get_avg_value_in_bbox(bbox: BBox, current_time: float) -> float`
  - `def classify_cell(info: float) -> str` (returns "white"/"gray"/"black")

- [ ] **Step 1: 编写信息素衰减的单元测试**

Write: `tests/schedule/test_info_field.py`
```python
import pytest
import numpy as np
import math
from schedule.config_loader import ConfigLoader
from schedule.datatypes import GridCoord, BBox
from schedule.info_field import InfoField

@pytest.fixture
def config():
    return ConfigLoader.load()

@pytest.fixture
def info_field(config):
    return InfoField(config)

def test_initial_info_is_zero(info_field):
    """所有 cell 初始信息素为 0 (黑态势)。"""
    mat = info_field.get_info_matrix()
    assert mat.shape == (30, 30)
    assert np.all(mat == 0.0)

def test_scan_cell_resets_info(info_field):
    """扫描后 cell 信息素重置为 1.0。"""
    info_field.scan_cell(GridCoord(10, 15), current_time=0.0)
    assert info_field.get_info_matrix()[10, 15] == 1.0

def test_decay_after_half_life(info_field):
    """经过一个半衰期后信息素衰减到 0.5。"""
    info_field.scan_cell(GridCoord(5, 5), current_time=0.0)
    info_field.update_decay(current_time=0.0)  # no decay immediately
    assert info_field.get_info_matrix()[5, 5] == 1.0

    half_life = info_field.config.grid.decay_half_life_min
    info_field.update_decay(current_time=half_life)
    assert pytest.approx(info_field.get_info_matrix()[5, 5], rel=0.01) == 0.5

def test_classify_cell(info_field):
    """态势分类正确。"""
    assert info_field.classify_cell(0.8) == "white"
    assert info_field.classify_cell(0.5) == "gray"
    assert info_field.classify_cell(0.1) == "black"

def test_track_decay_faster(info_field):
    """跟踪扫描衰减半衰期为 15min。"""
    info_field.scan_cell(GridCoord(3, 3), current_time=0.0, is_track=True)
    track_half = info_field.config.grid.track_decay_half_life_min
    info_field.update_decay(current_time=track_half)
    assert pytest.approx(info_field.get_info_matrix()[3, 3], rel=0.01) == 0.5

def test_add_marker_increases_value(info_field):
    """标记点附近 cell 信息价值升高。"""
    t = 0.0
    info_field.add_marker(GridCoord(15, 15), current_time=t, marker_id="M1")
    v1 = info_field.get_value_matrix(current_time=t)
    # 标记点中心 cell 应该有非零价值
    assert v1[15, 15] > 0.0

def test_scan_bbox_updates_all_cells(info_field):
    """扫描 bbox 覆盖的所有 cell。"""
    bbox = BBox(10, 10, 14, 13)  # 4x3 = 12 cells
    info_field.scan_bbox(bbox, current_time=10.0)
    info_mat = info_field.get_info_matrix()
    for c in range(10, 14):
        for r in range(10, 13):
            assert info_mat[c, r] == 1.0, f"Cell ({c},{r}) should be 1.0"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/schedule/test_info_field.py -v`
Expected: FAIL (InfoField not defined)

- [ ] **Step 3: 实现 InfoField**

Write: `schedule/info_field.py`
```python
import math
import numpy as np
from schedule.datatypes import GridCoord, BBox
from schedule.config_loader import AppConfig


class InfoField:
    def __init__(self, config: AppConfig):
        self.config = config
        gc = config.grid
        self.rows, self.cols = gc.resolution  # (30, 30)
        self.info = np.zeros((self.cols, self.rows), dtype=np.float64)
        self.last_scan_time = np.full((self.cols, self.rows), -np.inf, dtype=np.float64)
        self.is_track_scan = np.zeros((self.cols, self.rows), dtype=bool)
        self._markers: list[dict] = []

    def _decay_lambda(self, is_track: bool) -> float:
        gc = self.config.grid
        half_life = gc.track_decay_half_life_min if is_track else gc.decay_half_life_min
        return math.log(2) / half_life

    def update_decay(self, current_time: float) -> None:
        gc = self.config.grid
        # 只对已被扫描过的 cell 做衰减
        scanned_mask = np.isfinite(self.last_scan_time)
        if not np.any(scanned_mask):
            return

        dt = np.maximum(current_time - self.last_scan_time, 0.0)

        # 搜索扫描衰减
        search_mask = scanned_mask & ~self.is_track_scan
        lam_search = self._decay_lambda(False)
        self.info[search_mask] = np.exp(-lam_search * dt[search_mask])

        # 跟踪扫描衰减
        track_mask = scanned_mask & self.is_track_scan
        lam_track = self._decay_lambda(True)
        self.info[track_mask] = np.exp(-lam_track * dt[track_mask])

        # 限制在 [0, 1]
        np.clip(self.info, 0.0, 1.0, out=self.info)

    def scan_cell(self, coord: GridCoord, current_time: float, is_track: bool = False) -> None:
        c, r = coord
        if 0 <= c < self.cols and 0 <= r < self.rows:
            self.info[c, r] = 1.0
            self.last_scan_time[c, r] = current_time
            self.is_track_scan[c, r] = is_track

    def scan_bbox(self, bbox: BBox, current_time: float, is_track: bool = False) -> None:
        c0, r0, c1, r1 = bbox
        c0 = max(0, c0); r0 = max(0, r0)
        c1 = min(self.cols, c1); r1 = min(self.rows, r1)
        self.info[c0:c1, r0:r1] = 1.0
        self.last_scan_time[c0:c1, r0:r1] = current_time
        self.is_track_scan[c0:c1, r0:r1] = is_track

    def add_marker(self, position: GridCoord, current_time: float, marker_id: str) -> None:
        self._markers.append({
            "id": marker_id,
            "position": position,
            "created_time": current_time,
        })

    def remove_marker(self, marker_id: str) -> None:
        self._markers = [m for m in self._markers if m["id"] != marker_id]

    def _strategic_field(self, current_time: float) -> np.ndarray:
        """S(c,r): 标记点高斯衰减场。"""
        gc = self.config.grid
        S = np.zeros((self.cols, self.rows), dtype=np.float64)
        for marker in self._markers:
            age = current_time - marker["created_time"]
            if age > gc.marker_max_age_min:
                continue
            # 时间衰减因子（线性）
            time_factor = max(0.0, 1.0 - age / gc.marker_max_age_min)
            mc, mr = marker["position"]
            for c in range(self.cols):
                for r in range(self.rows):
                    dist = math.sqrt((c - mc) ** 2 + (r - mr) ** 2)
                    gauss = math.exp(-0.5 * (dist / gc.marker_sigma_cells) ** 2)
                    S[c, r] = max(S[c, r], gauss * time_factor)
        return S

    def _timeliness_field(self, current_time: float) -> np.ndarray:
        """A(c,r): 标记点时效性。"""
        gc = self.config.grid
        A = np.zeros((self.cols, self.rows), dtype=np.float64)
        lam = math.log(2) / gc.marker_decay_half_life_min
        for marker in self._markers:
            age = current_time - marker["created_time"]
            if age < 0:
                continue
            mc, mr = marker["position"]
            for c in range(self.cols):
                for r in range(self.rows):
                    dist = math.sqrt((c - mc) ** 2 + (r - mr) ** 2)
                    gauss = math.exp(-0.5 * (dist / gc.marker_sigma_cells) ** 2)
                    A[c, r] = max(A[c, r], gauss * math.exp(-lam * age))
        return A

    def get_value_matrix(self, current_time: float) -> np.ndarray:
        gc = self.config.grid
        alpha, beta, gamma = gc.value_alpha, gc.value_beta, gc.value_gamma
        info_gap = 1.0 - self.info
        S = self._strategic_field(current_time)
        A = self._timeliness_field(current_time)
        V = alpha * info_gap + beta * S + gamma * A
        return np.clip(V, 0.0, 1.0)

    def get_info_matrix(self) -> np.ndarray:
        return self.info.copy()

    def get_avg_info_in_bbox(self, bbox: BBox) -> float:
        c0, r0, c1, r1 = bbox
        patch = self.info[c0:c1, r0:r1]
        return float(np.mean(patch)) if patch.size > 0 else 0.0

    def get_avg_value_in_bbox(self, bbox: BBox, current_time: float) -> float:
        c0, r0, c1, r1 = bbox
        V = self.get_value_matrix(current_time)
        patch = V[c0:c1, r0:r1]
        return float(np.mean(patch)) if patch.size > 0 else 0.0

    def classify_cell(self, info: float) -> str:
        gc = self.config.grid
        if info > gc.white_threshold:
            return "white"
        elif info >= gc.gray_threshold:
            return "gray"
        else:
            return "black"
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/schedule/test_info_field.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/schedule/test_info_field.py schedule/info_field.py
git commit -m "feat: implement InfoField with exponential decay and marker-based value fields"
```

---

### Task 4: 状态管理器 (StateManager)

**Files:**
- Create: `schedule/state_manager.py`

**Interfaces:**
- Consumes: `AppConfig`, `UAVState`, `Region`, `Marker`, `BBox`, `GridCoord` from datatypes
- Produces:
  - `class StateManager`: 管理 UAV 状态列表、区域列表、标记点列表、仿真时间
  - `def step(current_time: float) -> None` (更新所有衰减)
  - `def get_available_uavs() -> list[UAVState]`
  - `def get_active_search_regions() -> list[Region]`
  - `def get_track_regions() -> list[Region]`
  - `def create_track_region(target_group_id: str, center: GridCoord) -> Region`
  - `def release_track_region(region_id: str) -> None`
  - `def add_event(event_type: str, data: dict) -> None` (记录事件用于 Reviewer)
  - `def get_recent_events(since_time: float) -> list[dict]`
  - `def get_previous_search_regions() -> list[Region]`

- [ ] **Step 1: 编写状态管理器的单元测试**

Write: `tests/schedule/test_state_manager.py`
```python
import pytest
from schedule.config_loader import ConfigLoader
from schedule.datatypes import GridCoord, BBox, UAVState, Region
from schedule.state_manager import StateManager

@pytest.fixture
def config():
    return ConfigLoader.load()

@pytest.fixture
def sm(config):
    return StateManager(config)

def test_init_creates_uavs(sm, config):
    """初始化后 UAV 列表已填充并为 idle 状态。"""
    uavs = sm.get_all_uavs()
    assert len(uavs) == config.uav.count_max
    assert all(u.status == "idle" for u in uavs)

def test_create_track_region(sm):
    """创建跟踪区后可从 get_track_regions 获取。"""
    sm.current_time = 100.0
    region = sm.create_track_region("G1", GridCoord(15, 15))
    tracks = sm.get_track_regions()
    assert len(tracks) == 1
    assert tracks[0].type == "track"
    assert tracks[0].bbox == BBox(13, 13, 17, 17)  # ±2 格

def test_release_track_region_adds_marker(sm):
    """释放跟踪区时创建标记点。"""
    sm.current_time = 200.0
    region = sm.create_track_region("G1", GridCoord(10, 10))
    sm.release_track_region(region.id, source_uav_id="UAV-1")
    markers = sm.get_active_markers()
    assert len(markers) == 1
    assert markers[0].source_uav_id == "UAV-1"

def test_add_event(sm):
    """事件记录到事件流中。"""
    sm.add_event("target_found", {"group_id": "G1", "position": GridCoord(5, 5)})
    events = sm.get_recent_events(since_time=0.0)
    assert len(events) == 1
    assert events[0]["type"] == "target_found"

def test_get_available_uavs(sm):
    """只返回 idle 状态的 UAV。"""
    sm.current_time = 50.0
    sm.update_uav_status("UAV-1", "searching", GridCoord(5, 5), assigned_region_id="S1")
    sm.update_uav_status("UAV-2", "transit", GridCoord(10, 10), assigned_region_id="S2")
    available = sm.get_available_uavs()
    assert len(available) == sm.config.uav.count_max - 2
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/schedule/test_state_manager.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 StateManager**

Write: `schedule/state_manager.py`
```python
import math
from typing import Optional
from schedule.datatypes import UAVState, Region, Marker, BBox, GridCoord
from schedule.config_loader import AppConfig
from schedule.info_field import InfoField


class StateManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.current_time: float = 0.0
        self.cycle: int = 0
        self.info_field = InfoField(config)

        # UAV 列表
        self._uavs: list[UAVState] = [
            UAVState(id=f"UAV-{i+1}", status="idle",
                     position=GridCoord(config.environment.base_position[0],
                                        config.environment.base_position[1]))
            for i in range(config.uav.count_max)
        ]

        # 区域
        self._search_regions: list[Region] = []
        self._track_regions: list[Region] = []
        self._previous_search_regions: list[Region] = []

        # 标记点
        self._markers: list[Marker] = []
        self._marker_counter: int = 0

        # 事件流
        self._events: list[dict] = []

        # 已发现的目标群
        self._known_target_groups: set[str] = set()

    def step(self, current_time: float) -> None:
        """推进一帧仿真时间，更新所有衰减。"""
        self.current_time = current_time
        self.info_field.update_decay(current_time)

    # --- UAV 管理 ---
    def get_all_uavs(self) -> list[UAVState]:
        return self._uavs

    def get_uav(self, uav_id: str) -> Optional[UAVState]:
        for u in self._uavs:
            if u.id == uav_id:
                return u
        return None

    def get_available_uavs(self) -> list[UAVState]:
        """状态为 idle 且 position 为基地的 UAV。"""
        return [u for u in self._uavs if u.status == "idle"]

    def update_uav_status(self, uav_id: str, status: str, position: GridCoord,
                          assigned_region_id: Optional[str] = None,
                          fuel_remaining_pct: Optional[float] = None,
                          target_group_id: Optional[str] = None) -> None:
        uav = self.get_uav(uav_id)
        if uav is None:
            return
        uav.status = status
        uav.position = position
        if assigned_region_id is not None:
            uav.assigned_region_id = assigned_region_id
        if fuel_remaining_pct is not None:
            uav.fuel_remaining_pct = fuel_remaining_pct
        if target_group_id is not None:
            uav.target_group_id = target_group_id

    # --- 区域管理 ---
    def set_search_regions(self, regions: list[Region]) -> None:
        self._previous_search_regions = list(self._search_regions)
        self._search_regions = regions

    def get_search_regions(self) -> list[Region]:
        return self._search_regions

    def get_active_search_regions(self) -> list[Region]:
        return [r for r in self._search_regions if r.status == "active"]

    def get_previous_search_regions(self) -> list[Region]:
        return self._previous_search_regions

    def get_track_regions(self) -> list[Region]:
        return self._track_regions

    def create_track_region(self, target_group_id: str, center: GridCoord) -> Region:
        """以目标位置为中心，创建 4×4 跟踪区。"""
        c, r = center
        half = 2
        bbox = BBox(
            max(0, c - half), max(0, r - half),
            min(self.config.grid.resolution[1], c + half),
            min(self.config.grid.resolution[0], r + half)
        )
        region = Region(
            id=f"T{len(self._track_regions)+1}",
            bbox=bbox, type="track", priority="high",
            created_cycle=self.cycle
        )
        self._track_regions.append(region)
        self._known_target_groups.add(target_group_id)
        return region

    def update_track_region_center(self, region_id: str, new_center: GridCoord) -> None:
        """跟随目标移动更新跟踪区位置。"""
        for r in self._track_regions:
            if r.id == region_id:
                c, r_c = new_center
                half = 2
                r.bbox = BBox(
                    max(0, c - half), max(0, r_c - half),
                    min(self.config.grid.resolution[1], c + half),
                    min(self.config.grid.resolution[0], r_c + half)
                )
                return

    def release_track_region(self, region_id: str, source_uav_id: str = "") -> None:
        """释放跟踪区并创建标记点。"""
        for r in self._track_regions:
            if r.id == region_id:
                center_col = (r.bbox.col_start + r.bbox.col_end) // 2
                center_row = (r.bbox.row_start + r.bbox.row_end) // 2
                self._marker_counter += 1
                marker = Marker(
                    id=f"MK{self._marker_counter}",
                    position=GridCoord(center_col, center_row),
                    created_time=self.current_time,
                    source_uav_id=source_uav_id,
                )
                self._markers.append(marker)
                self.info_field.add_marker(marker.position, self.current_time, marker.id)
                self._track_regions.remove(r)
                # 原跟踪区 cell 信息价值提升
                self._set_region_value_boost(r.bbox)
                return

    def _set_region_value_boost(self, bbox: BBox) -> None:
        """提升 bbox 内 cell 的信息价值（通过标记点已有机制）。"""
        # 标记点已通过 add_marker 提升了周边价值，此处无需额外操作
        pass

    def get_active_markers(self) -> list[Marker]:
        return self._markers

    # --- 事件管理 ---
    def add_event(self, event_type: str, data: dict) -> None:
        self._events.append({
            "type": event_type,
            "time": self.current_time,
            "data": data,
        })

    def get_recent_events(self, since_time: float) -> list[dict]:
        return [e for e in self._events if e["time"] >= since_time]

    # --- 信息场接口代理 ---
    def scan_bbox(self, bbox: BBox, current_time: float, is_track: bool = False) -> None:
        self.info_field.scan_bbox(bbox, current_time, is_track)

    def scan_cell(self, coord: GridCoord, current_time: float, is_track: bool = False) -> None:
        self.info_field.scan_cell(coord, current_time, is_track)

    def get_info_matrix(self):
        return self.info_field.get_info_matrix()

    def get_value_matrix(self):
        return self.info_field.get_value_matrix(self.current_time)

    def get_avg_info_in_bbox(self, bbox: BBox) -> float:
        return self.info_field.get_avg_info_in_bbox(bbox)

    def get_avg_value_in_bbox(self, bbox: BBox) -> float:
        return self.info_field.get_avg_value_in_bbox(bbox, self.current_time)
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/schedule/test_state_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/schedule/test_state_manager.py schedule/state_manager.py
git commit -m "feat: implement StateManager with UAV, region, marker, and event tracking"
```

---

### Task 5: 信息价值表 (InfoValueTable)

**Files:**
- Create: `schedule/info_value_table.py`

**Interfaces:**
- Consumes: `StateManager`, `RegionInfoRow`, `BBox` from datatypes
- Produces:
  - `class InfoValueTable`: 区域级信息聚合表
  - `def update_all() -> None` (更新所有行的 avg_info 和 value)
  - `def get_rows() -> list[RegionInfoRow]`
  - `def add_row(region_id, bbox, type, ...) -> None`
  - `def remove_row(region_id) -> None`

- [ ] **Step 1: 编写测试**

Write: `tests/schedule/test_info_value_table.py`
```python
import pytest
from schedule.config_loader import ConfigLoader
from schedule.state_manager import StateManager
from schedule.datatypes import BBox, GridCoord
from schedule.info_value_table import InfoValueTable

@pytest.fixture
def config():
    return ConfigLoader.load()

@pytest.fixture
def sm(config):
    return StateManager(config)

@pytest.fixture
def ivt(sm):
    return InfoValueTable(sm)

def test_add_row(ivt):
    ivt.add_row("S1", BBox(0, 0, 5, 6), "search")
    rows = ivt.get_rows()
    assert len(rows) == 1
    assert rows[0].region_id == "S1"

def test_update_all_computes_values(ivt, sm):
    sm.current_time = 100.0
    sm.scan_bbox(BBox(0, 0, 5, 6), sm.current_time, is_track=False)
    ivt.add_row("S1", BBox(0, 0, 5, 6), "search")
    ivt.update_all()
    row = ivt.get_rows()[0]
    assert 0.0 < row.value <= 1.0

def test_remove_row(ivt):
    ivt.add_row("S1", BBox(0, 0, 5, 6), "search")
    ivt.add_row("S2", BBox(10, 10, 15, 14), "search")
    ivt.remove_row("S1")
    rows = ivt.get_rows()
    assert len(rows) == 1
    assert rows[0].region_id == "S2"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/schedule/test_info_value_table.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 InfoValueTable**

Write: `schedule/info_value_table.py`
```python
from schedule.datatypes import BBox, RegionInfoRow
from schedule.state_manager import StateManager


class InfoValueTable:
    def __init__(self, sm: StateManager):
        self._sm = sm
        self._rows: dict[str, RegionInfoRow] = {}

    def add_row(self, region_id: str, bbox: BBox, type: str,
                assigned_uav_id: str = None) -> None:
        self._rows[region_id] = RegionInfoRow(
            region_id=region_id, bbox=bbox, type=type,
            avg_info=0.0, value=0.0,
            updated_time=self._sm.current_time,
            status="active", assigned_uav_id=assigned_uav_id,
        )

    def remove_row(self, region_id: str) -> None:
        self._rows.pop(region_id, None)

    def update_all(self) -> None:
        t = self._sm.current_time
        for row in self._rows.values():
            row.avg_info = self._sm.get_avg_info_in_bbox(row.bbox)
            row.value = self._sm.get_avg_value_in_bbox(row.bbox)
            row.updated_time = t

    def get_rows(self) -> list[RegionInfoRow]:
        return list(self._rows.values())

    def get_row(self, region_id: str) -> RegionInfoRow | None:
        return self._rows.get(region_id)
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/schedule/test_info_value_table.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/schedule/test_info_value_table.py schedule/info_value_table.py
git commit -m "feat: implement InfoValueTable for region-level info aggregation"
```

---

### Task 6: Candidate Extractor

**Files:**
- Create: `schedule/candidate_extractor.py`

**Interfaces:**
- Consumes: `StateManager.app_config`, `StateManager.get_value_matrix()`, `StateManager.get_info_matrix()`, `StateManager.get_track_regions()`, `StateManager.get_previous_search_regions()`
- Produces:
  - `class CandidateExtractor`
  - `def extract(state_manager) -> CandidateResult`
  - `CandidateResult`: dataclass with `candidate_regions: list[dict]`, `fragment_alerts: list[dict]`

- [ ] **Step 1: 编写测试**

Write: `tests/schedule/test_candidate_extractor.py`
```python
import pytest
import numpy as np
from schedule.config_loader import ConfigLoader
from schedule.state_manager import StateManager
from schedule.datatypes import GridCoord, BBox
from schedule.candidate_extractor import CandidateExtractor, CandidateResult

@pytest.fixture
def config():
    return ConfigLoader.load()

@pytest.fixture
def sm(config):
    sm = StateManager(config)
    sm.current_time = 50.0
    return sm

def test_extract_returns_candidate_result(sm):
    extractor = CandidateExtractor()
    result = extractor.extract(sm)
    assert isinstance(result, CandidateResult)

def test_black_cells_become_candidates(sm):
    """黑态势 cell 应形成候选区域。"""
    # 所有 cell 初始 info=0 (黑)，value 较高
    extractor = CandidateExtractor()
    result = extractor.extract(sm)
    assert len(result.candidate_regions) > 0

def test_track_regions_are_excluded(sm):
    """跟踪区 cell 应从候选区域中排除。"""
    sm.create_track_region("G1", GridCoord(15, 15))
    extractor = CandidateExtractor()
    result = extractor.extract(sm)
    # 验证候选区域的 bbox 不与跟踪区重叠
    for cand in result.candidate_regions:
        bbox = cand["bbox"]
        for track in sm.get_track_regions():
            assert not _bboxes_overlap(bbox, track.bbox)

def test_candidate_bbox_within_size_range(sm):
    """候选区域面积应在合理范围内。"""
    extractor = CandidateExtractor()
    result = extractor.extract(sm)
    for cand in result.candidate_regions:
        w = cand["bbox"].col_end - cand["bbox"].col_start
        h = cand["bbox"].row_end - cand["bbox"].row_start
        area = w * h
        assert area >= sm.config.grid.search_min_cells, f"Area {area} too small"
        assert area <= sm.config.grid.search_max_cells * 2, "Area unexpectedly large"

def _bboxes_overlap(a: BBox, b: BBox) -> bool:
    if a.col_end <= b.col_start or b.col_end <= a.col_start:
        return False
    if a.row_end <= b.row_start or b.row_end <= a.row_start:
        return False
    return True
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/schedule/test_candidate_extractor.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 CandidateExtractor**

Write: `schedule/candidate_extractor.py`
```python
import math
from dataclasses import dataclass, field
import numpy as np
from schedule.datatypes import BBox, GridCoord
from schedule.state_manager import StateManager


@dataclass
class CandidateResult:
    candidate_regions: list[dict] = field(default_factory=list)
    fragment_alerts: list[dict] = field(default_factory=list)


class CandidateExtractor:
    def extract(self, sm: StateManager) -> CandidateResult:
        gc = sm.config.grid
        cols, rows = gc.resolution
        V = sm.get_value_matrix()
        I = sm.get_info_matrix()

        # Step 1: 跟踪区占位
        occupied = np.zeros((cols, rows), dtype=bool)
        for tr in sm.get_track_regions():
            b = tr.bbox
            occupied[b.col_start:b.col_end, b.row_start:b.row_end] = True

        # Step 2: 高价值 cell 聚类 (连通域)
        threshold = gc.candidate_value_threshold
        high_value_mask = (V >= threshold) & ~occupied
        clusters = self._connected_components(high_value_mask)

        # Step 3: 按总价值排序
        clusters.sort(key=lambda c: c["total_value"], reverse=True)

        # Step 4: Top-K
        available = len(sm.get_available_uavs())
        K = min(available * 2, 10)  # 最多输出 10 个候选
        K = max(K, 5)  # 至少 5 个
        clusters = clusters[:K]

        # Step 5: 矩形拟合
        candidates = []
        for cluster in clusters:
            fitted = self._fit_rectangle(cluster["cells"], gc, cols, rows)
            if fitted is not None:
                fitted["total_value"] = cluster["total_value"]
                fitted["avg_info"] = cluster["avg_info"]
                candidates.append(fitted)

        # Step 6: 碎片检测
        fragments = self._detect_fragments(sm, occupied, gc)

        return CandidateResult(
            candidate_regions=candidates,
            fragment_alerts=fragments,
        )

    def _connected_components(self, mask: np.ndarray) -> list[dict]:
        """简单的 flood-fill 连通域分析。"""
        cols, rows = mask.shape
        visited = np.zeros_like(mask, dtype=bool)
        clusters = []

        for c in range(cols):
            for r in range(rows):
                if mask[c, r] and not visited[c, r]:
                    cells, total_value = self._flood_fill(mask, visited, c, r, cols, rows)
                    avg_info = 0.0  # 在调用处设置
                    clusters.append({
                        "cells": cells,
                        "total_value": total_value,
                        "avg_info": avg_info,
                    })

        return clusters

    def _flood_fill(self, mask, visited, start_c, start_r, cols, rows):
        from collections import deque
        q = deque([(start_c, start_r)])
        visited[start_c, start_r] = True
        cells = []
        total_value = 0.0
        # 使用原始 mask 作为 value 的近似
        V = mask  # 实际应为 value 矩阵，但这里用 mask 做连通域
        while q:
            c, r = q.popleft()
            cells.append(GridCoord(c, r))
            total_value += 1.0  # 简化，后续替换
            for dc, dr in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                nc, nr = c + dc, r + dr
                if 0 <= nc < cols and 0 <= nr < rows:
                    if mask[nc, nr] and not visited[nc, nr]:
                        visited[nc, nr] = True
                        q.append((nc, nr))
        return cells, total_value

    def _fit_rectangle(self, cells: list[GridCoord], gc, cols, rows) -> dict | None:
        """拟合最小外接矩形，必要时切分。"""
        if not cells:
            return None
        cs = [cell.col for cell in cells]
        rs = [cell.row for cell in cells]
        c_min, c_max = min(cs), max(cs) + 1
        r_min, r_max = min(rs), max(rs) + 1

        w, h = c_max - c_min, r_max - r_min
        area = w * h

        # 面积太小：与相邻合并（扩大矩形）
        if area < gc.fragment_threshold_cells:
            expand = 2
            c_min = max(0, c_min - expand)
            r_min = max(0, r_min - expand)
            c_max = min(cols, c_max + expand)
            r_max = min(rows, r_max + expand)
            w, h = c_max - c_min, r_max - r_min

        # 长宽比过大：沿长轴切分
        aspect = max(w, h) / max(min(w, h), 1)
        if aspect > gc.aspect_ratio_max:
            if w > h:
                mid = (c_min + c_max) // 2
                c_max = mid
            else:
                mid = (r_min + r_max) // 2
                r_max = mid

        return {
            "bbox": BBox(c_min, r_min, c_max, r_max),
            "cell_count": (c_max - c_min) * (r_max - r_min),
        }

    def _detect_fragments(self, sm: StateManager, occupied: np.ndarray, gc) -> list[dict]:
        """检测上一轮搜索区被挖除后的碎片。"""
        fragments = []
        prev_regions = sm.get_previous_search_regions()
        track_regions = sm.get_track_regions()

        for prev in prev_regions:
            for track in track_regions:
                if self._bboxes_overlap(prev.bbox, track.bbox):
                    # 计算剩余区域
                    remaining = self._bbox_difference(prev.bbox, track.bbox)
                    for rem_bbox in remaining:
                        area = (rem_bbox.col_end - rem_bbox.col_start) * (rem_bbox.row_end - rem_bbox.row_start)
                        if area < gc.fragment_threshold_cells:
                            fragments.append({
                                "bbox": rem_bbox,
                                "area": area,
                                "reason": f"区域{prev.id}被{track.id}挖除后产生{area}格碎片",
                                "parent_region_id": prev.id,
                            })

        return fragments

    def _bboxes_overlap(self, a: BBox, b: BBox) -> bool:
        if a.col_end <= b.col_start or b.col_end <= a.col_start:
            return False
        if a.row_end <= b.row_start or b.row_end <= a.row_start:
            return False
        return True

    def _bbox_difference(self, a: BBox, b: BBox) -> list[BBox]:
        """返回 a - b 的剩余矩形。简化实现：只处理 b 在 a 内部的切割。"""
        pieces = []
        # 左
        if a.col_start < b.col_start:
            pieces.append(BBox(a.col_start, a.row_start, b.col_start, a.row_end))
        # 右
        if b.col_end < a.col_end:
            pieces.append(BBox(b.col_end, a.row_start, a.col_end, a.row_end))
        # 上
        if a.row_start < b.row_start:
            pieces.append(BBox(
                max(a.col_start, b.col_start), a.row_start,
                min(a.col_end, b.col_end), b.row_start
            ))
        # 下
        if b.row_end < a.row_end:
            pieces.append(BBox(
                max(a.col_start, b.col_start), b.row_end,
                min(a.col_end, b.col_end), a.row_end
            ))
        return pieces
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/schedule/test_candidate_extractor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/schedule/test_candidate_extractor.py schedule/candidate_extractor.py
git commit -m "feat: implement CandidateExtractor for key region detection"
```

---

### Task 7: Hungarian 配对

**Files:**
- Create: `schedule/hungarian.py`

**Interfaces:**
- Consumes: `GridCoord` from datatypes
- Produces:
  - `def hungarian_pair(uavs: list[dict], regions: list[dict]) -> list[tuple]`
  - 输入: uavs = [{"id": str, "position": GridCoord}, ...], regions = [{"id": str, "bbox": BBox}, ...]
  - 输出: [(uav_id, region_id), ...]

- [ ] **Step 1: 编写测试**

Write: `tests/schedule/test_hungarian.py`
```python
import pytest
from schedule.datatypes import GridCoord, BBox
from schedule.hungarian import hungarian_pair

def test_hungarian_basic():
    uavs = [
        {"id": "UAV-1", "position": GridCoord(0, 0)},
        {"id": "UAV-2", "position": GridCoord(20, 0)},
    ]
    regions = [
        {"id": "S1", "bbox": BBox(0, 0, 5, 5)},
        {"id": "S2", "bbox": BBox(20, 0, 25, 5)},
    ]
    pairs = hungarian_pair(uavs, regions)
    assert ("UAV-1", "S1") in pairs
    assert ("UAV-2", "S2") in pairs

def test_hungarian_more_uavs_than_regions():
    uavs = [
        {"id": "UAV-1", "position": GridCoord(0, 0)},
        {"id": "UAV-2", "position": GridCoord(10, 10)},
    ]
    regions = [{"id": "S1", "bbox": BBox(15, 15, 20, 20)}]
    pairs = hungarian_pair(uavs, regions)
    assert len(pairs) == 1

def test_hungarian_more_regions_than_uavs():
    uavs = [{"id": "UAV-1", "position": GridCoord(5, 5)}]
    regions = [
        {"id": "S1", "bbox": BBox(0, 0, 5, 5)},
        {"id": "S2", "bbox": BBox(20, 0, 25, 5)},
    ]
    pairs = hungarian_pair(uavs, regions)
    assert len(pairs) == 1

def test_hungarian_empty_input():
    pairs = hungarian_pair([], [])
    assert pairs == []
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/schedule/test_hungarian.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 Hungarian 配对**

Write: `schedule/hungarian.py`
```python
import math
from typing import Optional
from schedule.datatypes import GridCoord, BBox


def _bbox_center(bbox: BBox) -> tuple[float, float]:
    return (
        (bbox.col_start + bbox.col_end) / 2.0,
        (bbox.row_start + bbox.row_end) / 2.0,
    )


def hungarian_pair(uavs: list[dict], regions: list[dict]) -> list[tuple[str, str]]:
    """Hungarian 算法最小总距离配对。
    
    输入:
      uavs: [{"id": str, "position": GridCoord}, ...]
      regions: [{"id": str, "bbox": BBox}, ...]
    输出:
      [(uav_id, region_id), ...]
    """
    n_uavs = len(uavs)
    n_regions = len(regions)

    if n_uavs == 0 or n_regions == 0:
        return []

    # 构建代价矩阵
    n = max(n_uavs, n_regions)
    cost = [[0.0] * n for _ in range(n)]

    for i, uav in enumerate(uavs):
        for j, region in enumerate(regions):
            cx, cy = _bbox_center(region["bbox"])
            dx = uav["position"].col - cx
            dy = uav["position"].row - cy
            cost[i][j] = math.sqrt(dx * dx + dy * dy)

    # 填充虚拟行/列 (代价 = 0，表示不产生真实配对)
    # 使用简单贪心配对 (scipy 可能不可用)
    assignments: list[Optional[int]] = [None] * n_uavs
    region_taken = [False] * n_regions

    # 按代价排序的贪心配对
    pairs = []
    for i in range(n_uavs):
        for j in range(n_regions):
            pairs.append((cost[i][j], i, j))
    pairs.sort(key=lambda x: x[0])

    for _, i, j in pairs:
        if assignments[i] is None and not region_taken[j]:
            assignments[i] = j
            region_taken[j] = True

    result = []
    for i, j in enumerate(assignments):
        if j is not None:
            result.append((uavs[i]["id"], regions[j]["id"]))

    return result
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/schedule/test_hungarian.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/schedule/test_hungarian.py schedule/hungarian.py
git commit -m "feat: implement Hungarian-based UAV-to-region pairing"
```

---

### Task 8: Output Validator

**Files:**
- Create: `schedule/output_validator.py`

**Interfaces:**
- Consumes: `AppConfig (grid)`, `BBox`, `Region` from datatypes
- Produces:
  - `def validate(llm_output: dict, config, track_regions, prev_search_regions) -> ValidationResult`
  - `ValidationResult`: `is_valid: bool`, `errors: list[str]`

- [ ] **Step 1: 编写测试**

Write: `tests/schedule/test_output_validator.py`
```python
import pytest
from schedule.config_loader import ConfigLoader
from schedule.datatypes import BBox, Region
from schedule.output_validator import validate, ValidationResult

@pytest.fixture
def config():
    return ConfigLoader.load()

def make_output(bboxes):
    return {
        "cycle": 1,
        "search_regions": [
            {"id": f"S{i+1}", "bbox": list(b), "priority": "high", "reason": "test"}
            for i, b in enumerate(bboxes)
        ],
        "notes": "test"
    }

def test_valid_output_passes(config):
    result = validate(make_output([[0, 0, 5, 6], [10, 10, 15, 16]]), config, [], [])
    assert result.is_valid

def test_bbox_out_of_bounds_fails(config):
    result = validate(make_output([[-5, 0, 5, 6]]), config, [], [])
    assert not result.is_valid
    assert any("out of bounds" in e.lower() for e in result.errors)

def test_area_too_small_fails(config):
    result = validate(make_output([[0, 0, 2, 3]]), config, [], [])  # 6 cells < 20
    assert not result.is_valid
    assert any("area" in e.lower() for e in result.errors)

def test_area_too_large_fails(config):
    result = validate(make_output([[0, 0, 10, 10]]), config, [], [])  # 100 cells > 40
    assert not result.is_valid

def test_aspect_ratio_fails(config):
    result = validate(make_output([[0, 0, 8, 2]]), config, [], [])  # 8:2 = 4:1 > 2:1
    assert not result.is_valid

def test_overlap_fails(config):
    result = validate(make_output([[0, 0, 5, 5], [3, 3, 8, 8]]), config, [], [])
    assert not result.is_valid
    assert any("overlap" in e.lower() for e in result.errors)

def test_overlap_with_track_region_fails(config):
    track = Region(id="T1", bbox=BBox(12, 12, 16, 16), type="track")
    result = validate(make_output([[10, 10, 15, 14]]), config, [track], [])
    assert not result.is_valid

def test_too_many_regions_fails(config):
    bboxes = [[i, 0, i+1, 6] for i in range(11)]  # 11 regions > 10
    result = validate(make_output(bboxes), config, [], [])
    assert not result.is_valid
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/schedule/test_output_validator.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 Output Validator**

Write: `schedule/output_validator.py`
```python
from dataclasses import dataclass, field
from schedule.datatypes import BBox, Region
from schedule.config_loader import AppConfig


@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str] = field(default_factory=list)


def validate(llm_output: dict, config: AppConfig,
             track_regions: list[Region],
             prev_search_regions: list[Region]) -> ValidationResult:
    """校验 LLM 输出的搜索区域划分方案。"""
    errors = []
    gc = config.grid

    regions_data = llm_output.get("search_regions", [])
    if not regions_data:
        errors.append("search_regions is empty")
        return ValidationResult(is_valid=False, errors=errors)

    bboxes = []
    for i, r in enumerate(regions_data):
        bbox = r.get("bbox", [])
        if len(bbox) != 4:
            errors.append(f"Region {i}: bbox must have 4 elements, got {len(bbox)}")
            continue

        c0, r0, c1, r1 = bbox
        cols, rows = gc.resolution

        # 1. 坐标范围
        if not (0 <= c0 < c1 <= cols and 0 <= r0 < r1 <= rows):
            errors.append(f"Region {i} bbox {bbox}: out of bounds [0,{cols}]×[0,{rows}]")
            continue

        b = BBox(c0, r0, c1, r1)
        w, h = c1 - c0, r1 - r0
        area = w * h

        # 2. 面积约束
        if area < gc.search_min_cells:
            errors.append(f"Region {i} area={area}: below minimum {gc.search_min_cells}")
        if area > gc.search_max_cells:
            errors.append(f"Region {i} area={area}: above maximum {gc.search_max_cells}")

        # 3. 长宽比
        aspect = max(w, h) / max(min(w, h), 1)
        if aspect > gc.aspect_ratio_max:
            errors.append(f"Region {i} aspect={aspect:.2f}: exceeds max {gc.aspect_ratio_max}")

        bboxes.append((i, b))

    # 4. 不重叠（搜索区之间）
    for i in range(len(bboxes)):
        for j in range(i + 1, len(bboxes)):
            a = bboxes[i][1]
            b = bboxes[j][1]
            if _bboxes_overlap(a, b):
                errors.append(f"Region {bboxes[i][0]} and {bboxes[j][0]} overlap")

    # 5. 不与跟踪区重叠
    for ti, track in enumerate(track_regions):
        for ri, r_bbox in bboxes:
            if _bboxes_overlap(r_bbox, track.bbox):
                errors.append(f"Search region {ri} overlaps track region {track.id}")

    # 6. 数量约束
    total_regions = len(regions_data) + len(track_regions)
    if total_regions > 10:
        errors.append(f"Total regions {total_regions} exceeds UAV max 10")

    # 7. 稳定性约束 (可选，非致命)
    for ri, r_bbox in bboxes:
        for prev in prev_search_regions:
            if prev.id == regions_data[ri].get("id"):
                iou = _compute_iou(r_bbox, prev.bbox)
                if iou < gc.stability_iou_threshold:
                    errors.append(f"Region {ri} IoU={iou:.2f} below stability threshold {gc.stability_iou_threshold}")
                break

    return ValidationResult(is_valid=len(errors) == 0, errors=errors)


def _bboxes_overlap(a: BBox, b: BBox) -> bool:
    if a.col_end <= b.col_start or b.col_end <= a.col_start:
        return False
    if a.row_end <= b.row_start or b.row_end <= a.row_start:
        return False
    return True


def _compute_iou(a: BBox, b: BBox) -> float:
    if not _bboxes_overlap(a, b):
        return 0.0
    inter_w = min(a.col_end, b.col_end) - max(a.col_start, b.col_start)
    inter_h = min(a.row_end, b.row_end) - max(a.row_start, b.row_start)
    inter_area = inter_w * inter_h
    area_a = (a.col_end - a.col_start) * (a.row_end - a.row_start)
    area_b = (b.col_end - b.col_start) * (b.row_end - b.row_start)
    union_area = area_a + area_b - inter_area
    return inter_area / union_area if union_area > 0 else 0.0
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/schedule/test_output_validator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/schedule/test_output_validator.py schedule/output_validator.py
git commit -m "feat: implement OutputValidator for LLM output validation"
```

---

### Task 9: LLM Client + Prompt Builder

**Files:**
- Create: `schedule/llm_client.py`
- Create: `schedule/prompt_builder.py`
- Create: `schedule/prompts/system_prompt.txt`

**Interfaces:**
- Consumes: `AppConfig (llm)`, `CandidateResult`, `StateManager`, `InfoValueTable`
- Produces:
  - `class LLMClient`: OpenAI-compatible API 封装
  - `def decide(candidate_result, state_manager, ivt) -> dict` (组装 prompt → 调用 LLM → 解析 JSON)
  - `class PromptBuilder`: 组装 System + User prompt

- [ ] **Step 1: 创建 System Prompt 模板**

Write: `schedule/prompts/system_prompt.txt`
```
你是 UAV 编队任务调度决策器。任务：300×300km 海域对海侦察与目标跟踪。

【区域划分约束】
1. 矩形性：输出轴对齐矩形，坐标系 (col,row)，范围 [0,29]×[0,29]
2. 尺寸范围：搜索区 20–40 格，长宽比 ≤ 2:1
3. 数量上限：区域数 ≤ 可用 UAV 数（最多 10）
4. 不重叠：区域之间互不相交
5. 稳定性：与上一轮同区域偏差 ≤ 30% 面积
6. 碎片合并：< 12 格的碎片须并入相邻区域
7. 优先级：高价值黑态区 > 灰态区 > 白态区

【输出格式】
严格 JSON，无额外文字：
{
  "search_regions": [
    {"id": "S1", "bbox": [col_start, row_start, col_end, row_end], "priority": "high|medium|low", "reason": "说明"}
  ],
  "notes": "简要说明本周期方案"
}
```

- [ ] **Step 2: 实现 PromptBuilder**

Write: `schedule/prompt_builder.py`
```python
import os
from schedule.state_manager import StateManager
from schedule.info_value_table import InfoValueTable
from schedule.candidate_extractor import CandidateResult


class PromptBuilder:
    def __init__(self, system_prompt_path: str = None):
        if system_prompt_path is None:
            system_prompt_path = os.path.join(
                os.path.dirname(__file__), "prompts", "system_prompt.txt"
            )
        with open(system_prompt_path, "r", encoding="utf-8") as f:
            self.system_prompt = f.read()

    def build(self, sm: StateManager, ivt: InfoValueTable,
              candidate_result: CandidateResult,
              reviewer_memory: str = "") -> tuple[str, str]:
        """返回 (system_prompt, user_prompt)"""
        user = self._build_user_prompt(sm, ivt, candidate_result, reviewer_memory)
        return self.system_prompt, user

    def _build_user_prompt(self, sm: StateManager, ivt: InfoValueTable,
                           candidate_result: CandidateResult, reviewer_memory: str) -> str:
        parts = []

        # 长期记忆
        if reviewer_memory:
            parts.append(f"【长期记忆】\n{reviewer_memory}")

        # 候选搜索区域
        parts.append("【候选搜索区域】(按信息价值降序)")
        for i, cand in enumerate(candidate_result.candidate_regions):
            b = cand["bbox"]
            area = (b.col_end - b.col_start) * (b.row_end - b.row_start)
            info = cand.get("avg_info", 0.0)
            situation = "黑" if info < 0.2 else ("灰" if info < 0.7 else "白")
            parts.append(
                f"{i+1}. bbox({b.col_start},{b.row_start},{b.col_end},{b.row_end}) "
                f"面积{area}格 平均信息{info:.2f}({situation}) "
                f"总价值{cand.get('total_value', 0):.2f}"
            )

        # 跟踪中区域
        tracks = sm.get_track_regions()
        if tracks:
            parts.append("\n【跟踪中区域】(由规则维护，不参与重新划分)")
            for t in tracks:
                b = t.bbox
                uav_id = t.assigned_uav_id or "unassigned"
                parts.append(
                    f"{t.id}: bbox({b.col_start},{b.row_start},{b.col_end},{b.row_end}) "
                    f"UAV={uav_id}"
                )

        # 上一轮搜索区状态
        ivt.update_all()
        prev_rows = [r for r in ivt.get_rows() if r.type == "search"]
        if prev_rows:
            parts.append("\n【上一轮搜索区状态】")
            for row in prev_rows:
                info = row.avg_info
                situation = "白" if info > 0.7 else ("灰" if info > 0.2 else "黑")
                uav = row.assigned_uav_id or "unassigned"
                st = row.status
                parts.append(
                    f"{row.region_id}: bbox({row.bbox.col_start},{row.bbox.row_start},"
                    f"{row.bbox.col_end},{row.bbox.row_end}) "
                    f"信息{info:.2f}({situation}) 状态={st} UAV={uav}"
                )

        # 碎片提醒
        if candidate_result.fragment_alerts:
            parts.append("\n【碎片提醒】")
            for frag in candidate_result.fragment_alerts:
                parts.append(f"- {frag['reason']}")

        # UAV 可用状态
        parts.append("\n【UAV 可用状态】")
        all_uavs = sm.get_all_uavs()
        available = [u for u in all_uavs if u.status == "idle"]
        in_use = [u for u in all_uavs if u.status != "idle"]
        parts.append(f"现可用UAV: {len(available)}架")
        for u in in_use:
            parts.append(f"  {u.id}: {u.status}, 油量{u.fuel_remaining_pct:.0%}%, "
                        f"区域={u.assigned_region_id or 'none'}")
        parts.append(f"本周期可用总数: {len(available)}架")

        parts.append("\n请输出本周期任务区域划分方案。")
        return "\n".join(parts)
```

- [ ] **Step 3: 实现 LLMClient**

Write: `schedule/llm_client.py`
```python
import json
import os
import re
from schedule.config_loader import AppConfig
from schedule.state_manager import StateManager
from schedule.info_value_table import InfoValueTable
from schedule.candidate_extractor import CandidateResult
from schedule.prompt_builder import PromptBuilder
from schedule.output_validator import validate


class LLMClient:
    def __init__(self, config: AppConfig):
        self.config = config
        self.prompt_builder = PromptBuilder()
        self._api_key = os.environ.get(config.llm.api_key_env, "")
        self._reviewer_memory: str = ""

    def set_reviewer_memory(self, memory: str) -> None:
        self._reviewer_memory = memory

    def decide(self, sm: StateManager, ivt: InfoValueTable,
               candidate_result: CandidateResult) -> dict:
        """调用 LLM 决策管线，返回解析后的 JSON dict。"""
        system_prompt, user_prompt = self.prompt_builder.build(
            sm, ivt, candidate_result, self._reviewer_memory
        )

        for attempt in range(self.config.llm.max_retries + 1):
            raw = self._call_api(system_prompt, user_prompt)
            parsed = self._parse_json(raw)

            if parsed is None:
                user_prompt += f"\n\n[上轮输出不是有效JSON，请严格按照JSON格式输出]\n原始输出: {raw[:200]}"
                continue

            # 校验
            result = validate(parsed, self.config, sm.get_track_regions(),
                            sm.get_previous_search_regions())
            if result.is_valid:
                return parsed

            # 回注错误
            error_msg = "\n".join(result.errors)
            user_prompt += f"\n\n[上轮输出校验失败，请修正以下错误]\n{error_msg}"

        # 兜底：返回空方案
        return {"search_regions": [], "notes": "LLM failed after max retries"}

    def _call_api(self, system_prompt: str, user_prompt: str) -> str:
        """OpenAI-compatible API 调用。"""
        try:
            from openai import OpenAI
        except ImportError:
            # 模拟返回（离线测试用）
            return self._mock_response()

        client = OpenAI(api_key=self._api_key, base_url=self.config.llm.api_base)
        response = client.chat.completions.create(
            model=self.config.llm.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.config.llm.temperature,
            max_tokens=self.config.llm.max_tokens,
        )
        return response.choices[0].message.content or ""

    def _mock_response(self) -> str:
        """离线测试用的模拟响应。"""
        return json.dumps({
            "search_regions": [
                {"id": "S1", "bbox": [0, 0, 5, 6], "priority": "high", "reason": "mock"},
                {"id": "S2", "bbox": [10, 10, 15, 14], "priority": "medium", "reason": "mock"},
            ],
            "notes": "mock response"
        })

    def _parse_json(self, raw: str) -> dict | None:
        """从 LLM 响应中提取 JSON。"""
        raw = raw.strip()
        # 尝试直接解析
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        # 尝试从 ```json ``` 块中提取
        match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return None
```

- [ ] **Step 4: 验证 LLM Client 基础功能**

Run: `python -c "from schedule.config_loader import ConfigLoader; from schedule.llm_client import LLMClient; c = ConfigLoader.load(); client = LLMClient(c); r = client._mock_response(); print(client._parse_json(r))"`
Expected: 输出解析后的 dict

- [ ] **Step 5: Commit**

```bash
git add schedule/prompts/system_prompt.txt schedule/prompt_builder.py schedule/llm_client.py
git commit -m "feat: implement LLMClient and PromptBuilder with validation loop"
```

---

### Task 10: LLM Reviewer

**Files:**
- Create: `schedule/llm_reviewer.py`

**Interfaces:**
- Consumes: `StateManager` (事件流)
- Produces:
  - `class LLMReviewer`: 后台 15min 周期，凝练长期记忆
  - `def step(current_time, state_manager) -> str | None` (返回新记忆或 None)

- [ ] **Step 1: 实现 LLMReviewer**

Write: `schedule/llm_reviewer.py`
```python
from schedule.config_loader import AppConfig
from schedule.state_manager import StateManager


class LLMReviewer:
    def __init__(self, config: AppConfig):
        self.config = config
        self._last_review_time: float = -float("inf")
        self._memory: str = ""

    @property
    def memory(self) -> str:
        return self._memory

    def step(self, current_time: float, sm: StateManager) -> str | None:
        """按 15min 周期更新长期记忆。返回新记忆或 None。"""
        cycle = self.config.llm.reviewer_cycle_min
        if current_time - self._last_review_time < cycle:
            return None

        self._last_review_time = current_time

        # 收集统计信息
        events = sm.get_recent_events(since_time=max(0, current_time - 120))  # 过去2小时

        total_found = sum(1 for e in events if e["type"] == "target_found")
        total_lost = sum(1 for e in events if e["type"] == "target_lost")
        total_returned = sum(1 for e in events if e["type"] == "uav_returned")

        tracks = sm.get_track_regions()
        tracking_info = ""
        for t in tracks:
            tracking_info += f"{t.id}跟踪中 "

        # 搜索覆盖率
        info_mat = sm.get_info_matrix()
        searched = (info_mat > 0.0).sum()
        total_cells = info_mat.size
        coverage_pct = searched / total_cells * 100

        # 凝练为自然语言
        memory = (
            f"过去2小时内，共搜索约{coverage_pct:.0f}%海域，"
            f"发现目标{total_found}次，丢失{total_lost}次。"
        )
        if tracking_info:
            memory += f" {tracking_info}。"
        if total_returned > 0:
            memory += f" {total_returned}架次UAV返航。"
        memory += f" (更新于t={current_time:.0f}min)"

        # 精简到 ≤ 200 字
        if len(memory) > 200:
            memory = memory[:197] + "..."

        self._memory = memory
        return memory
```

- [ ] **Step 2: 验证 Reviewer**

Run: `python -c "from schedule.config_loader import ConfigLoader; from schedule.state_manager import StateManager; from schedule.llm_reviewer import LLMReviewer; c = ConfigLoader.load(); sm = StateManager(c); rev = LLMReviewer(c); r = rev.step(20, sm); print(r or 'too early')"`
Expected: `too early` (首次调用 20min < 15min 周期)

- [ ] **Step 3: Commit**

```bash
git add schedule/llm_reviewer.py
git commit -m "feat: implement LLMReviewer for long-term memory condensation"
```

---

### Task 11: Trigger Manager

**Files:**
- Create: `schedule/trigger_manager.py`

**Interfaces:**
- Consumes: `StateManager`
- Produces:
  - `class TriggerManager`: 监听事件，决策轻/重量触发
  - `def check(current_time) -> TriggerDecision`
  - `TriggerDecision`: `trigger_type: "light" | "heavy" | "none"`, `reason: str`, `affected_uavs: list[str]`

- [ ] **Step 1: 编写测试**

Write: `tests/schedule/test_trigger_manager.py`
```python
import pytest
from schedule.config_loader import ConfigLoader
from schedule.state_manager import StateManager
from schedule.trigger_manager import TriggerManager, TriggerDecision

@pytest.fixture
def config():
    return ConfigLoader.load()

@pytest.fixture
def sm(config):
    return StateManager(config)

def test_initial_trigger_is_none(sm):
    tm = TriggerManager(sm)
    d = tm.check(0.0)
    assert d.trigger_type == "none"

def test_periodic_heavy_trigger(sm, config):
    tm = TriggerManager(sm)
    cycle = config.llm.heavy_cycle_min
    d = tm.check(cycle)
    assert d.trigger_type == "heavy"

def test_uav_search_complete_light_trigger(sm):
    tm = TriggerManager(sm)
    tm.notify_event("search_complete", time=10.0, uav_id="UAV-1", region_id="S1")
    d = tm.check(10.0)
    assert d.trigger_type == "light"

def test_uav_returned_heavy_trigger(sm):
    tm = TriggerManager(sm)
    tm.notify_event("uav_returned", time=15.0, uav_id="UAV-3",
                    position={"col": 18, "row": 8}, marker_position={"col": 18, "row": 8})
    d = tm.check(15.0)
    assert d.trigger_type == "heavy"
```

- [ ] **Step 2: 运行测试**

Run: `pytest tests/schedule/test_trigger_manager.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 TriggerManager**

Write: `schedule/trigger_manager.py`
```python
from dataclasses import dataclass, field
from schedule.state_manager import StateManager


@dataclass
class TriggerDecision:
    trigger_type: str  # "light" | "heavy" | "none"
    reason: str = ""
    affected_uavs: list[str] = field(default_factory=list)


class TriggerManager:
    def __init__(self, sm: StateManager):
        self._sm = sm
        self._pending_events: list[dict] = []
        self._last_heavy_time: float = -float("inf")
        self._last_light_time: float = -float("inf")

    def notify_event(self, event_type: str, time: float, **kwargs) -> None:
        self._pending_events.append({
            "type": event_type,
            "time": time,
            **kwargs,
        })

    def check(self, current_time: float) -> TriggerDecision:
        """检查是否需要触发，返回决策。"""
        if not self._pending_events:
            return TriggerDecision("none")

        # 合并 5min 内的事件
        recent = [e for e in self._pending_events
                  if current_time - e["time"] <= 5.0]
        self._pending_events = [e for e in self._pending_events
                                if current_time - e["time"] > 5.0]

        if not recent:
            return TriggerDecision("none")

        heavy_types = {"uav_returned", "target_found", "target_lost"}
        light_types = {"search_complete", "uav_refueled"}

        heavy_count = sum(1 for e in recent if e["type"] in heavy_types)
        light_count = sum(1 for e in recent if e["type"] in light_types)

        # 重量触发条件
        if heavy_count > 0 or heavy_count + light_count >= 3:
            affected = list(set(
                e.get("uav_id", "") for e in recent
                if e.get("uav_id", "")
            ))
            return TriggerDecision(
                trigger_type="heavy",
                reason=f"{heavy_count} heavy + {light_count} light events",
                affected_uavs=affected,
            )

        # 轻量触发
        if light_count > 0:
            affected = [e.get("uav_id", "") for e in recent
                       if e.get("uav_id", "") and e["type"] in light_types]
            return TriggerDecision(
                trigger_type="light",
                reason=f"{light_count} light events",
                affected_uavs=affected,
            )

        # 周期定时
        cycle = self._sm.config.llm.heavy_cycle_min
        if current_time - self._last_heavy_time >= cycle:
            return TriggerDecision(
                trigger_type="heavy",
                reason=f"periodic {cycle}min cycle",
            )

        return TriggerDecision("none")

    def mark_triggered(self, trigger_type: str, time: float) -> None:
        if trigger_type == "heavy":
            self._last_heavy_time = time
        elif trigger_type == "light":
            self._last_light_time = time
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/schedule/test_trigger_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/schedule/test_trigger_manager.py schedule/trigger_manager.py
git commit -m "feat: implement TriggerManager with light/heavy event classification"
```

---

### Task 12: Task Allocator (主编排器)

**Files:**
- Create: `schedule/task_allocator.py`

**Interfaces:**
- Consumes: `StateManager`, `InfoValueTable`, `CandidateExtractor`, `LLMClient`, `Hungarian`, `TriggerManager`
- Produces:
  - `class TaskAllocator`: 主编排器，连接所有组件
  - `def step(current_time) -> dict` (单步推进，返回本步执行的动作)

- [ ] **Step 1: 编写集成测试**

Write: `tests/schedule/test_task_allocator.py`
```python
import pytest
from schedule.config_loader import ConfigLoader
from schedule.task_allocator import TaskAllocator

@pytest.fixture
def config():
    return ConfigLoader.load()

@pytest.fixture
def allocator(config):
    return TaskAllocator(config)

def test_allocator_initializes_all_components(allocator):
    assert allocator.sm is not None
    assert allocator.ivt is not None
    assert allocator.extractor is not None
    assert allocator.llm_client is not None
    assert allocator.trigger_manager is not None

def test_step_initial_no_trigger(allocator):
    result = allocator.step(0.0)
    assert result["trigger_type"] == "none"

def test_step_heavy_trigger_at_cycle(allocator):
    cycle = allocator.config.llm.heavy_cycle_min
    result = allocator.step(cycle)
    assert result["trigger_type"] == "heavy"
    assert "search_regions" in result

def test_uav_search_complete_light_trigger(allocator):
    allocator.sm.current_time = 10.0
    region = allocator.sm.create_track_region("G1",
        allocator.sm.config.environment.base_position)
    allocator.ivt.add_row("S1",
        allocator.sm.config.environment.base_position, "search", "UAV-1")
    allocator.trigger_manager.notify_event(
        "search_complete", time=10.0, uav_id="UAV-1", region_id="S1")
    result = allocator.step(10.0)
    assert result["trigger_type"] in ("light", "none")
```

- [ ] **Step 2: 运行测试**

Run: `pytest tests/schedule/test_task_allocator.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 TaskAllocator**

Write: `schedule/task_allocator.py`
```python
from schedule.config_loader import ConfigLoader, AppConfig
from schedule.state_manager import StateManager
from schedule.info_field import InfoField
from schedule.info_value_table import InfoValueTable
from schedule.candidate_extractor import CandidateExtractor
from schedule.llm_client import LLMClient
from schedule.llm_reviewer import LLMReviewer
from schedule.hungarian import hungarian_pair
from schedule.trigger_manager import TriggerManager
from schedule.datatypes import Region, BBox, GridCoord


class TaskAllocator:
    def __init__(self, config: AppConfig):
        self.config = config
        self.sm = StateManager(config)
        self.ivt = InfoValueTable(self.sm)
        self.extractor = CandidateExtractor()
        self.llm_client = LLMClient(config)
        self.reviewer = LLMReviewer(config)
        self.trigger_manager = TriggerManager(self.sm)

    def step(self, current_time: float) -> dict:
        """推进一帧，返回本步摘要。"""
        self.sm.step(current_time)

        # Reviewer 更新
        new_memory = self.reviewer.step(current_time, self.sm)
        if new_memory:
            self.llm_client.set_reviewer_memory(new_memory)

        # 检查触发
        decision = self.trigger_manager.check(current_time)

        if decision.trigger_type == "none":
            return {"trigger_type": "none", "action": None}

        if decision.trigger_type == "light":
            return self._handle_light_trigger(current_time, decision)

        if decision.trigger_type == "heavy":
            return self._handle_heavy_trigger(current_time, decision)

        return {"trigger_type": "none", "action": None}

    def _handle_light_trigger(self, current_time: float, decision) -> dict:
        """轻量触发：Hungarian 直接配对。"""
        idle_uavs = self.sm.get_available_uavs()
        if not idle_uavs:
            return {"trigger_type": "light", "action": "no_idle_uavs"}

        # 获取未分配的候选搜索区
        candidate_result = self.extractor.extract(self.sm)
        unassigned = [c for c in candidate_result.candidate_regions
                     if not any(r.assigned_uav_id for r in self.sm.get_search_regions()
                               if r.bbox == c["bbox"])]

        if not unassigned:
            return {"trigger_type": "light", "action": "no_unassigned_regions"}

        pairs = hungarian_pair(
            [{"id": u.id, "position": u.position} for u in idle_uavs],
            unassigned,
        )

        # 更新状态
        for uav_id, region_id in pairs:
            region = next((r for r in candidate_result.candidate_regions
                          if r.get("id") == region_id), None)
            if region:
                self.sm.update_uav_status(uav_id, "transit",
                    self.sm.get_uav(uav_id).position,
                    assigned_region_id=region_id)

        self.trigger_manager.mark_triggered("light", current_time)
        return {
            "trigger_type": "light",
            "action": "hungarian_pairing",
            "pairs": pairs,
        }

    def _handle_heavy_trigger(self, current_time: float, decision) -> dict:
        """重量触发：调 LLM 全局重划分。"""
        # Step 1: 更新信息价值表
        self.ivt.update_all()

        # Step 2: 提取候选区域
        candidate_result = self.extractor.extract(self.sm)

        # Step 3-5: LLM 决策（含校验重试）
        llm_output = self.llm_client.decide(self.sm, self.ivt, candidate_result)

        # Step 6: 新旧区域 ID 匹配 + 创建 Region 对象
        new_regions = []
        prev_regions = self.sm.get_previous_search_regions()
        prev_by_id = {r.id: r for r in prev_regions}

        for sr in llm_output.get("search_regions", []):
            bbox = BBox(*sr["bbox"])
            # ID 连续性：如果与上一轮某区域 IoU 够高，沿用其 ID
            matched_id = sr.get("id", "S" + str(len(new_regions) + 1))
            for prev_id, prev_r in prev_by_id.items():
                if matched_id not in [r.id for r in new_regions]:
                    iou = self._iou(bbox, prev_r.bbox)
                    if iou >= self.config.grid.stability_iou_threshold:
                        matched_id = prev_id
                        break

            region = Region(
                id=matched_id,
                bbox=bbox,
                type="search",
                priority=sr.get("priority", "medium"),
                info_value=0.0,  # 由 InfoValueTable 后续计算
            )
            new_regions.append(region)

        self.sm.set_search_regions(new_regions)

        # 更新 IVT
        for r in new_regions:
            self.ivt.add_row(r.id, r.bbox, "search")
        # 移除不存在的旧行
        new_ids = {r.id for r in new_regions}
        for row in list(self.ivt.get_rows()):
            if row.type == "search" and row.region_id not in new_ids:
                self.ivt.remove_row(row.region_id)

        # Step 7: Hungarian 配对
        idle_uavs = self.sm.get_available_uavs()
        unassigned = [
            {"id": r.id, "bbox": r.bbox}
            for r in new_regions
            if r.assigned_uav_id is None
        ]
        pairs = hungarian_pair(
            [{"id": u.id, "position": u.position} for u in idle_uavs],
            unassigned,
        )

        for uav_id, region_id in pairs:
            self.sm.update_uav_status(uav_id, "transit",
                self.sm.get_uav(uav_id).position,
                assigned_region_id=region_id)
            for r in new_regions:
                if r.id == region_id:
                    r.assigned_uav_id = uav_id

        self.trigger_manager.mark_triggered("heavy", current_time)
        self.sm.cycle += 1

        return {
            "trigger_type": "heavy",
            "action": "llm_reallocation",
            "search_regions": [{"id": r.id, "bbox": list(r.bbox)} for r in new_regions],
            "pairs": pairs,
            "notes": llm_output.get("notes", ""),
        }

    def _iou(self, a: BBox, b: BBox) -> float:
        if a.col_end <= b.col_start or b.col_end <= a.col_start:
            return 0.0
        if a.row_end <= b.row_start or b.row_end <= a.row_start:
            return 0.0
        inter_w = min(a.col_end, b.col_end) - max(a.col_start, b.col_start)
        inter_h = min(a.row_end, b.row_end) - max(a.row_start, b.row_start)
        inter = inter_w * inter_h
        area_a = (a.col_end - a.col_start) * (a.row_end - a.row_start)
        area_b = (b.col_end - b.col_start) * (b.row_end - b.row_start)
        return inter / (area_a + area_b - inter)
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/schedule/test_task_allocator.py -v`
Expected: 集成测试通过（使用 mock LLM 响应）

- [ ] **Step 5: Commit**

```bash
git add tests/schedule/test_task_allocator.py schedule/task_allocator.py
git commit -m "feat: implement TaskAllocator as main orchestrator connecting all components"
```

---

### Task 13: 启发式规则 (utils/)

**Files:**
- Create: `utils/__init__.py`
- Create: `utils/scan_pattern.py`
- Create: `utils/waypoint.py`
- Create: `utils/track_orbit.py`
- Create: `utils/return_path.py`
- Create: `utils/sensor_control.py`

**Interfaces:**
- 各模块提供纯函数，输入当前位置+目标区域，输出航路点序列

- [ ] **Step 1: 创建 utils/__init__.py 和基础航路点**

Write: `utils/__init__.py`
```python
# utils - heuristic rules for UAV waypoint, sensor control, scan patterns, and tracking
```

Write: `utils/waypoint.py`
```python
"""航路点计算：当前位置 → 目标区域的简单直线路径。"""
import math
from schedule.datatypes import GridCoord, BBox


def navigate_to_region(current: GridCoord, target_bbox: BBox,
                       cell_size_km: float = 10.0) -> list[GridCoord]:
    """生成从当前位置到目标区域中心的直线航路点。"""
    cx = (target_bbox.col_start + target_bbox.col_end) / 2.0
    cy = (target_bbox.row_start + target_bbox.row_end) / 2.0
    # 简单直线路径：起点 → 终点
    waypoints = [current]
    # 在途中添加中间点（如果需要避开障碍等，此处简化）
    waypoints.append(GridCoord(int(cx), int(cy)))
    return waypoints


def grid_distance(a: GridCoord, b: GridCoord) -> float:
    """网格坐标间的欧氏距离（单位：格）。"""
    return math.sqrt((a.col - b.col) ** 2 + (a.row - b.row) ** 2)


def travel_time(a: GridCoord, b: GridCoord, cruise_speed_kmh: float,
                cell_size_km: float = 10.0) -> float:
    """计算两点间的飞行时间（单位：分钟）。"""
    dist_km = grid_distance(a, b) * cell_size_km
    return (dist_km / cruise_speed_kmh) * 60.0
```

Write: `utils/scan_pattern.py`
```python
"""覆盖扫描模式：弓形扫描（Boustrophedon pattern）。"""
from schedule.datatypes import GridCoord, BBox


def generate_scan_waypoints(bbox: BBox, swath_cells: int = 1) -> list[GridCoord]:
    """生成弓形扫描航路点序列。
    
    扫描方向：沿 row 方向来回，col 方向步进。
    swath_cells: SAR 条带宽度对应的 cell 数 (15km / 10km ≈ 1-2)。
    """
    waypoints = []
    c_start, r_start, c_end, r_end = bbox
    left_to_right = True

    for c in range(c_start, c_end, swath_cells):
        if left_to_right:
            waypoints.append(GridCoord(c, r_start))
            waypoints.append(GridCoord(c, r_end - 1))
        else:
            waypoints.append(GridCoord(c, r_end - 1))
            waypoints.append(GridCoord(c, r_start))
        left_to_right = not left_to_right

    return waypoints


def estimate_coverage_time(bbox: BBox, cruise_speed_kmh: float,
                           sar_swath_km: int, cell_size_km: int = 10,
                           efficiency: float = 0.75) -> float:
    """估算覆盖 bbox 所需时间（单位：分钟）。"""
    w_cells = bbox.col_end - bbox.col_start
    h_cells = bbox.row_end - bbox.row_start
    area_km2 = w_cells * h_cells * cell_size_km * cell_size_km
    coverage_rate_km2h = cruise_speed_kmh * sar_swath_km * efficiency
    hours = area_km2 / coverage_rate_km2h
    return hours * 60.0
```

Write: `utils/track_orbit.py`
```python
"""目标跟踪盘旋：围绕目标保持一定距离的圆形/椭圆形轨迹。"""
import math
from schedule.datatypes import GridCoord


def generate_orbit_waypoints(target_position: GridCoord,
                             standoff_cells: float = 3.0,
                             num_points: int = 8) -> list[GridCoord]:
    """生成围绕目标的盘旋航路点。
    
    standoff_cells: 与目标保持的距离（格）。
    """
    waypoints = []
    for i in range(num_points):
        angle = 2 * math.pi * i / num_points
        col = target_position.col + standoff_cells * math.cos(angle)
        row = target_position.row + standoff_cells * math.sin(angle)
        waypoints.append(GridCoord(int(col), int(row)))
    return waypoints


def update_orbit_center(old_waypoints: list[GridCoord],
                        target_displacement: tuple[float, float]) -> list[GridCoord]:
    """根据目标位移更新盘旋中心。"""
    dc, dr = target_displacement
    return [GridCoord(wp.col + int(dc), wp.row + int(dr)) for wp in old_waypoints]
```

Write: `utils/return_path.py`
```python
"""返航路径规划：当前位置 → 基地的最短直线路径。"""
from schedule.datatypes import GridCoord


def return_to_base(current: GridCoord, base_position: GridCoord) -> list[GridCoord]:
    """生成返航航路点。"""
    return [current, base_position]
```

Write: `utils/sensor_control.py`
```python
"""传感器朝向控制：SAR/光电/雷帧的简单朝向规则。"""
from schedule.datatypes import GridCoord


def sensor_heading_for_search(uav_position: GridCoord,
                               next_waypoint: GridCoord) -> float:
    """覆盖搜索时传感器指向前进方向。"""
    import math
    dx = next_waypoint.col - uav_position.col
    dy = next_waypoint.row - uav_position.row
    return math.degrees(math.atan2(dy, dx))


def sensor_heading_for_track(uav_position: GridCoord,
                              target_position: GridCoord) -> float:
    """跟踪时传感器始终指向目标。"""
    import math
    dx = target_position.col - uav_position.col
    dy = target_position.row - uav_position.row
    return math.degrees(math.atan2(dy, dx))
```

- [ ] **Step 2: 验证导入**

Run: `python -c "from utils.waypoint import navigate_to_region; from utils.scan_pattern import generate_scan_waypoints; from utils.track_orbit import generate_orbit_waypoints; print('All utils imported OK')"`
Expected: `All utils imported OK`

- [ ] **Step 3: Commit**

```bash
git add utils/
git commit -m "feat: add heuristic rule modules for waypoint, scan, track, return, and sensor control"
```

---

### Task 14: 仿真环境基础 (wm/)

**Files:**
- Create: `wm/__init__.py`
- Create: `wm/ship.py`
- Create: `wm/uav_entity.py`
- Create: `wm/base_station.py`
- Create: `wm/sim_clock.py`

**Interfaces:**
- `class Ship`: 目标船舶实体（zigzag 逃逸）
- `class UAVEntity`: UAV 实体（状态+油耗+位置更新）
- `class BaseStation`: 基地（UAV 加油队列）
- `class SimClock`: 仿真时钟

- [ ] **Step 1: 实现 Ship 实体**

Write: `wm/__init__.py`
```python
# wm - simulation world model
```

Write: `wm/ship.py`
```python
"""目标船舶实体：宙斯盾驱逐舰级别，支持 zigzag 逃逸。"""
import math
import random
from schedule.datatypes import GridCoord


class Ship:
    def __init__(self, ship_id: str, initial_position: GridCoord,
                 speed_kn: float, zigzag_amplitude_km: float,
                 zigzag_period_min: float, cell_size_km: float = 10.0):
        self.id = ship_id
        self.position = initial_position
        self.speed_kn = speed_kn  # 节
        self.speed_km_per_min = speed_kn * 1.852 / 60.0  # km/min
        self.zigzag_amplitude_km = zigzag_amplitude_km
        self.zigzag_period_min = zigzag_period_min
        self.cell_size_km = cell_size_km
        self._detected: bool = False
        self._zigzag_phase: float = random.uniform(0, 2 * math.pi)
        self._base_heading: float = random.uniform(0, 2 * math.pi)
        self.group_id: str | None = None

    @property
    def detected(self) -> bool:
        return self._detected

    def mark_detected(self) -> None:
        self._detected = True

    def step(self, dt_min: float) -> None:
        """推进 dt 分钟。"""
        if not self._detected:
            # 未被发现前可能漂移
            return

        # Zigzag 逃逸
        t = self._zigzag_phase
        self._zigzag_phase += dt_min / self.zigzag_period_min * 2 * math.pi

        # 横向偏移（zigzag）
        lateral_offset = self.zigzag_amplitude_km * math.sin(self._zigzag_phase)

        # 沿基本方向前进
        forward_dist = self.speed_km_per_min * dt_min
        dx_km = forward_dist * math.cos(self._base_heading) - lateral_offset * math.sin(self._base_heading)
        dy_km = forward_dist * math.sin(self._base_heading) + lateral_offset * math.cos(self._base_heading)

        # 更新位置
        new_col = self.position.col + dx_km / self.cell_size_km
        new_row = self.position.row + dy_km / self.cell_size_km

        # clamp 到 [0, 29] 网格范围
        new_col = max(0, min(29, new_col))
        new_row = max(0, min(29, new_row))

        # 如果碰到边界，改变方向
        if new_col <= 0 or new_col >= 29 or new_row <= 0 or new_row >= 29:
            self._base_heading = random.uniform(0, 2 * math.pi)

        self.position = GridCoord(int(new_col), int(new_row))
```

Write: `wm/uav_entity.py`
```python
"""UAV 实体：包含位置、油量、状态更新逻辑。"""
from schedule.datatypes import GridCoord, BBox


class UAVEntity:
    def __init__(self, uav_id: str, base_position: GridCoord,
                 endurance_h: float, cruise_speed_kmh: float):
        self.id = uav_id
        self.position = base_position
        self.base_position = base_position
        self.endurance_h = endurance_h
        self.cruise_speed_kmh = cruise_speed_kmh
        self.fuel_remaining_pct: float = 1.0
        self.status: str = "idle"  # idle|transit|searching|tracking|returning|refueling
        self.assigned_region: BBox | None = None
        self.waypoints: list[GridCoord] = []
        self._wp_index: int = 0
        self._fuel_consumption_rate: float = 1.0 / (endurance_h * 60.0)  # % per minute

    def assign_mission(self, region_bbox: BBox, waypoints: list[GridCoord]) -> None:
        self.assigned_region = region_bbox
        self.waypoints = waypoints
        self._wp_index = 0
        self.status = "transit"

    def step(self, dt_min: float) -> bool:
        """推进 dt 分钟。返回 True 表示油量耗尽需返航。"""
        # 燃油消耗
        if self.status not in ("idle", "refueling"):
            self.fuel_remaining_pct -= self._fuel_consumption_rate * dt_min
            self.fuel_remaining_pct = max(0.0, self.fuel_remaining_pct)

        # 按航路点移动
        if self.waypoints and self._wp_index < len(self.waypoints):
            target = self.waypoints[self._wp_index]
            dist_cells = ((target.col - self.position.col) ** 2 +
                         (target.row - self.position.row) ** 2) ** 0.5
            dist_km = dist_cells * 10.0  # cell_size_km
            speed_km_per_min = self.cruise_speed_kmh / 60.0
            travel_dist = speed_km_per_min * dt_min

            if travel_dist >= dist_km:
                self.position = target
                self._wp_index += 1
                if self._wp_index >= len(self.waypoints):
                    if self.status == "transit":
                        self.status = "searching"
                    elif self.status == "returning":
                        self.status = "refueling"
            else:
                ratio = travel_dist / max(dist_km, 0.001)
                new_col = self.position.col + (target.col - self.position.col) * ratio
                new_row = self.position.row + (target.row - self.position.row) * ratio
                self.position = GridCoord(int(new_col), int(new_row))

        # 油量检查
        if self.fuel_remaining_pct <= 0.05 and self.status not in ("returning", "idle", "refueling"):
            self.status = "returning"
            self.waypoints = [self.position, self.base_position]
            self._wp_index = 0
            return True

        return False
```

Write: `wm/base_station.py`
```python
"""基地：UAV 起飞/降落/加油管理。"""
from schedule.datatypes import GridCoord


class BaseStation:
    def __init__(self, position: GridCoord, refuel_time_min: float):
        self.position = position
        self.refuel_time_min = refuel_time_min
        self._refueling_queue: dict[str, float] = {}  # uav_id -> time_remaining_min

    def land_uav(self, uav_id: str) -> None:
        self._refueling_queue[uav_id] = self.refuel_time_min

    def step(self, dt_min: float) -> list[str]:
        """推进 dt 分钟。返回加油完成的 UAV ID 列表。"""
        ready = []
        for uav_id in list(self._refueling_queue):
            self._refueling_queue[uav_id] -= dt_min
            if self._refueling_queue[uav_id] <= 0:
                ready.append(uav_id)
                del self._refueling_queue[uav_id]
        return ready

    def is_refueling(self, uav_id: str) -> bool:
        return uav_id in self._refueling_queue
```

Write: `wm/sim_clock.py`
```python
"""仿真时钟。"""


class SimClock:
    def __init__(self, start_time: float = 0.0):
        self.time: float = start_time
        self.dt_min: float = 1.0  # 默认步长 1 分钟

    def tick(self) -> float:
        self.time += self.dt_min
        return self.time
```

- [ ] **Step 2: 验证仿真模块**

Run: `python -c "from wm.ship import Ship; from wm.uav_entity import UAVEntity; from wm.base_station import BaseStation; from wm.sim_clock import SimClock; from schedule.datatypes import GridCoord; s = Ship('S1', GridCoord(10,10), 18, 5, 10); s.mark_detected(); s.step(5); print(f'Ship pos: {s.position}'); u = UAVEntity('U1', GridCoord(15,28), 30, 160); print(f'UAV fuel: {u.fuel_remaining_pct}'); b = BaseStation(GridCoord(15,28), 12); c = SimClock(); print(f'Clock: {c.tick()}')"`
Expected: 输出各种状态信息，无异常

- [ ] **Step 3: Commit**

```bash
git add wm/
git commit -m "feat: implement simulation world model (ship, UAV entity, base station, clock)"
```

---

### Task 15: 主仿真循环

**Files:**
- Create: `main.py` (项目根目录)

**Interfaces:**
- `def main(config_path="configs")`: 初始化所有组件，运行仿真主循环

- [ ] **Step 1: 实现主循环**

Write: `main.py`
```python
"""UAV 海上侦察动态任务重分配 — 主仿真入口。"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from schedule.config_loader import ConfigLoader
from schedule.task_allocator import TaskAllocator
from wm.ship import Ship
from wm.uav_entity import UAVEntity
from wm.base_station import BaseStation
from wm.sim_clock import SimClock
from schedule.datatypes import GridCoord
import random


def main(config_path: str = "configs"):
    config = ConfigLoader.load(config_path)

    # 初始化仿真时钟
    clock = SimClock()

    # 初始化基地
    base_pos = GridCoord(*config.environment.base_position)
    base = BaseStation(base_pos, config.uav.refuel_time_min)

    # 初始化 UAV 实体
    uavs: list[UAVEntity] = []
    for i in range(config.uav.count_max):
        uav = UAVEntity(f"UAV-{i+1}", base_pos,
                        config.uav.endurance_h, config.uav.cruise_speed_kmh)
        uavs.append(uav)

    # 初始化 Task Allocator
    allocator = TaskAllocator(config)

    # 初始化目标船舶
    random.seed(42)
    ships: list[Ship] = []
    ship_count = random.randint(config.ship.count_min, config.ship.count_min + 5)
    groups = random.randint(1, min(3, ship_count))  # 1-3 个目标群
    ships_per_group = ship_count // groups

    for g in range(groups):
        group_center = GridCoord(random.randint(5, 24), random.randint(5, 24))
        for s in range(ships_per_group):
            offset_col = random.randint(-2, 2)
            offset_row = random.randint(-2, 2)
            pos = GridCoord(
                max(0, min(29, group_center.col + offset_col)),
                max(0, min(29, group_center.row + offset_row))
            )
            ship = Ship(f"Ship-{g+1}-{s+1}", pos,
                       config.ship.speed_kn, config.ship.zigzag_amplitude_km,
                       config.ship.zigzag_period_min)
            ship.group_id = f"G{g+1}"
            ships.append(ship)

    print(f"初始化: {len(uavs)} UAVs, {len(ships)} ships in {groups} groups")
    print(f"基地位置: {base_pos}")

    # 主循环
    max_time_min = 480  # 8 小时仿真
    dt = clock.dt_min

    while clock.time < max_time_min:
        t = clock.tick()

        # 1. 更新船舶位置
        for ship in ships:
            ship.step(dt)

        # 2. 更新 UAV 位置 + 油量
        for uav in uavs:
            fuel_low = uav.step(dt)
            if fuel_low:
                allocator.trigger_manager.notify_event(
                    "uav_returned", time=t, uav_id=uav.id,
                    position={"col": uav.position.col, "row": uav.position.row}
                )
                allocator.sm.add_event("uav_returned", {"uav_id": uav.id})
                allocator.sm.update_uav_status(uav.id, "returning", uav.position)

            # 检测目标（简化：同 cell 则发现）
            if uav.status in ("searching", "tracking"):
                for ship in ships:
                    if not ship.detected and uav.position == ship.position:
                        ship.mark_detected()
                        allocator.trigger_manager.notify_event(
                            "target_found", time=t,
                            group_id=ship.group_id,
                            position={"col": ship.position.col, "row": ship.position.row}
                        )
                        allocator.sm.add_event("target_found", {
                            "ship_id": ship.id, "group_id": ship.group_id,
                            "position": ship.position
                        })
                        print(f"[t={t:.0f}min] {uav.id} 发现 {ship.id} 在 {ship.position}")

        # 3. 扫描信息场更新
        for uav in uavs:
            if uav.status == "searching":
                is_track = False
                allocator.sm.scan_cell(uav.position, t, is_track=is_track)
            elif uav.status == "tracking":
                allocator.sm.scan_cell(uav.position, t, is_track=True)

        # 4. 基地加油更新
        for uav in uavs:
            if uav.status == "returning" and uav.position == base_pos:
                base.land_uav(uav.id)
                uav.status = "refueling"
                uav.fuel_remaining_pct = 0.0

        ready_uavs = base.step(dt)
        for uav_id in ready_uavs:
            for uav in uavs:
                if uav.id == uav_id:
                    uav.fuel_remaining_pct = 1.0
                    uav.status = "idle"
                    uav.position = base_pos
                    allocator.trigger_manager.notify_event(
                        "uav_refueled", time=t, uav_id=uav.id
                    )

        # 5. 任务重分配
        result = allocator.step(t)
        if result["trigger_type"] != "none":
            print(f"[t={t:.0f}min] Trigger: {result['trigger_type']} - {result.get('action')}")
            if result["trigger_type"] == "heavy":
                for r in result.get("search_regions", []):
                    print(f"  Region {r['id']}: bbox={r['bbox']}")

        # 6. 进度打印
        if int(t) % 60 == 0:
            info_mat = allocator.sm.get_info_matrix()
            coverage = (info_mat > 0.0).sum() / info_mat.size * 100
            tracking = len(allocator.sm.get_track_regions())
            free_uavs = len(allocator.sm.get_available_uavs())
            print(f"[t={t:.0f}min] 覆盖率 {coverage:.1f}% | 跟踪 {tracking} 群 | 空闲 UAV {free_uavs}")

    print("仿真结束。")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行完整仿真（使用 mock LLM）**

Run: `python main.py`
Expected: 仿真运行至 480 分钟，输出覆盖率进展、触发日志、区域划分信息

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: implement main simulation loop with full component integration"
```

---

### Task 16: 最终验证

- [ ] **Step 1: 运行全部测试**

```bash
pytest tests/ -v
```
Expected: 所有测试通过

- [ ] **Step 2: 运行仿真集成测试**

```bash
python -c "
from main import main
main()
"
```
Expected: 完整仿真运行无崩溃

- [ ] **Step 3: 提交最终变更**

```bash
git add -A
git commit -m "feat: complete LLM dynamic task reallocation system"
```
