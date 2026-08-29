"""Immutable public data contracts for UAV control strategies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

import numpy as np

from src.schedule.datatypes import BBox


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


class StopReason(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    PREEMPTED = "preempted"


Pose = tuple[float, float, float]


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
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ControlDecision:
    command: ControlCommand
    events: tuple[ControllerEventRequest, ...] = ()


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


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

    def __post_init__(self) -> None:
        for array in (
            self.local_info,
            self.local_value,
            self.obstacle_mask,
            self.searchable_mask,
            self.planning_obstacle_mask,
        ):
            array.setflags(write=False)


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
