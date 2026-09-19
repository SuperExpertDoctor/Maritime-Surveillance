"""Immutable public data contracts for UAV control strategies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Literal

import numpy as np

from src.schedule.datatypes import BBox
from src.mission.contracts import ContactSnapshot, ProbeSession


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
    PROBE = "probe"
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


class _FrozenMapping(Mapping):
    """Immutable mapping for contract payloads that also has to be copyable.

    ``types.MappingProxyType`` gives the immutability these snapshots need but
    cannot itself be deep-copied, and consumers do copy them: frame publication
    takes a ``deepcopy`` of the whole scheduler state so that historical frames
    are not mutated by later steps.  Copying a deeply immutable mapping is the
    identity operation, so ``__deepcopy__`` returns ``self``.
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping) -> None:
        self._data = {
            key: _immutable_snapshot(item) for key, item in data.items()
        }

    def __getitem__(self, key: object) -> object:
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"frozen_mapping({self._data!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return self._data == dict(other)
        return NotImplemented

    def __deepcopy__(self, memo: dict) -> "_FrozenMapping":
        return self


def _immutable_snapshot(value: object) -> object:
    if isinstance(value, Mapping):
        return _FrozenMapping(value)
    if isinstance(value, list | tuple):
        return tuple(_immutable_snapshot(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_immutable_snapshot(item) for item in value)
    return value


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
class CoverageExecutionConfig:
    """Physical SAR geometry shared by planning, safety, and execution."""

    swath_width_cells: float
    near_range_cells: float
    min_turn_radius_cells: float
    along_track_cells: float
    heading_tolerance_rad: float
    cross_track_tolerance_cells: float

    def __post_init__(self) -> None:
        positive = (
            "swath_width_cells",
            "near_range_cells",
            "min_turn_radius_cells",
            "along_track_cells",
        )
        for name in positive:
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        for name in ("heading_tolerance_rad", "cross_track_tolerance_cells"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class ActionMask:
    allowed_sensor_modes: tuple[SensorMode, ...]
    allowed_operation_modes: tuple[OperationMode, ...]
    target_contact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_sensor_modes", tuple(self.allowed_sensor_modes))
        object.__setattr__(
            self, "allowed_operation_modes", tuple(self.allowed_operation_modes)
        )
        object.__setattr__(self, "target_contact_ids", tuple(self.target_contact_ids))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "position", tuple(self.position))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "estimated_position", tuple(self.estimated_position))
        object.__setattr__(self, "estimated_velocity", tuple(self.estimated_velocity))


@dataclass(frozen=True)
class HazardObservation:
    hazard_id: str
    hazard_type: str
    center: tuple[float, float]
    half_extent_cells: float
    velocity_cells_min: tuple[float, float]
    intensity: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "center", tuple(self.center))
        object.__setattr__(self, "velocity_cells_min", tuple(self.velocity_cells_min))


@dataclass(frozen=True)
class BaseObservation:
    base_id: str
    position: tuple[float, float]
    capacity: int
    reserved_load: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "position", tuple(self.position))


@dataclass(frozen=True)
class ControlCommand:
    turn_rate_rad_min: float
    speed_cells_min: float
    sensor_mode: SensorMode
    operation_mode: OperationMode
    target_contact_id: str | None = None
    schema_version: str = "control-command/v1"
    sar_look_direction: Literal["left", "right"] | None = field(
        default=None, kw_only=True
    )
    sar_scan_heading_rad: float | None = field(default=None, kw_only=True)
    sar_scan_origin: tuple[float, float] | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class ControllerEventRequest:
    event_type: str
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _immutable_snapshot(self.payload))


@dataclass(frozen=True)
class ControlDecision:
    command: ControlCommand
    events: tuple[ControllerEventRequest, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))


@dataclass(frozen=True)
class RecoveryPlan:
    base_id: str
    base_position: tuple[float, float]
    reservation_id: str
    path: tuple[Pose, ...]
    path_length_cells: float
    reserve_cells: float
    planning_map_version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_position", tuple(self.base_position))
        object.__setattr__(self, "path", tuple(tuple(pose) for pose in self.path))


@dataclass(frozen=True)
class ControlTask:
    task_id: str
    task_type: OperationMode
    region_bbox: BBox | None = None
    target_contact_id: str | None = None
    probe_id: str | None = None
    recovery_plan: RecoveryPlan | None = None


_ROUTE_STATUSES = frozenset(
    {"ready", "pending", "guidance_only", "unavailable", "cleared"}
)


@dataclass(frozen=True)
class ControlRouteSnapshot:
    task_id: str | None
    task_type: str
    phase: str
    target_contact_id: str | None
    route: tuple[Pose, ...]
    next_index: int
    route_revision: int
    planning_map_version: int | None
    status: str
    coverage_progress: Mapping[str, object] | None = field(
        default=None, kw_only=True
    )

    def __post_init__(self) -> None:
        for name in ("task_type", "phase"):
            value = getattr(self, name)
            if isinstance(value, Enum):
                value = value.value
                object.__setattr__(self, name, value)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if self.task_id is not None and (
            not isinstance(self.task_id, str) or not self.task_id
        ):
            raise ValueError("task_id must be a non-empty string or None")
        if self.target_contact_id is not None and (
            not isinstance(self.target_contact_id, str) or not self.target_contact_id
        ):
            raise ValueError(
                "target_contact_id must be a non-empty string or None"
            )
        if self.status not in _ROUTE_STATUSES:
            raise ValueError(f"status must be one of {sorted(_ROUTE_STATUSES)}")
        if isinstance(self.next_index, bool) or not isinstance(self.next_index, int):
            raise ValueError("next_index must be an integer")
        if isinstance(self.route_revision, bool) or not isinstance(self.route_revision, int):
            raise ValueError("route_revision must be an integer")
        if self.route_revision < 0:
            raise ValueError("route_revision must be non-negative")
        if self.planning_map_version is not None and (
            isinstance(self.planning_map_version, bool)
            or not isinstance(self.planning_map_version, int)
            or self.planning_map_version < 0
        ):
            raise ValueError(
                "planning_map_version must be a non-negative integer or None"
            )
        if self.coverage_progress is not None:
            if not isinstance(self.coverage_progress, Mapping):
                raise ValueError("coverage_progress must be a mapping or None")
            object.__setattr__(
                self,
                "coverage_progress",
                _immutable_snapshot(self.coverage_progress),
            )
        normalized_route = []
        for pose in self.route:
            if not isinstance(pose, (tuple, list)) or len(pose) != 3:
                raise ValueError("route poses must be finite triples")
            normalized_pose = tuple(float(value) for value in pose)
            if not all(math.isfinite(value) for value in normalized_pose):
                raise ValueError("route poses must be finite triples")
            normalized_route.append(normalized_pose)
        if not 0 <= self.next_index <= len(normalized_route):
            raise ValueError("next_index must be between zero and route length")
        object.__setattr__(self, "route", tuple(normalized_route))


@dataclass(frozen=True)
class UavRouteSnapshot:
    episode_id: str
    generation: int
    route: ControlRouteSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.episode_id, str):
            raise ValueError("episode_id must be a string")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise ValueError("generation must be an integer")
        if self.generation < 0:
            raise ValueError("generation must be non-negative")
        if not isinstance(self.route, ControlRouteSnapshot):
            raise TypeError("route must be a ControlRouteSnapshot")


@dataclass(frozen=True)
class ControlEvent:
    sequence: int
    timestamp_min: float
    event_type: str
    source: str
    uav_id: str | None
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _immutable_snapshot(self.payload))


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
    probe: ProbeSession | None = None
    contact_histories: tuple[ContactSnapshot, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "local_info",
            "local_value",
            "obstacle_mask",
            "searchable_mask",
            "planning_obstacle_mask",
        ):
            array = np.array(getattr(self, field_name), copy=True)
            array.setflags(write=False)
            object.__setattr__(self, field_name, array)
        for field_name in (
            "contacts", "hazards", "bases", "shared_uavs", "events", "contact_histories"
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))


@dataclass(frozen=True)
class ControllerContext:
    uav_id: str
    dt_min: float
    observation_spec: ObservationSpec
    action_spec: ActionSpec
    episode_id: str
    task: ControlTask | None = None
    generation: int = field(default=0, kw_only=True)

    def __post_init__(self) -> None:
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise ValueError("generation must be an integer")
        if self.generation < 0:
            raise ValueError("generation must be non-negative")


@dataclass(frozen=True)
class PolicySource:
    uri: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _immutable_snapshot(self.metadata))
