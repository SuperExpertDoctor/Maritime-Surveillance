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
    status: str = "active"  # "active" | "completed" | "stale"
    priority: str = "medium"  # "high" | "medium" | "low"
    info_value: float = 0.0
    avg_info: float = 0.0
    assigned_uav_id: Optional[str] = None
    completion_pct: float = 0.0
    created_cycle: int = 0
    target_group_id: Optional[str] = None


@dataclass
class UAVState:
    id: str
    status: str  # "idle" | "transit" | "searching" | "tracking" | "returning" | "refueling"
    position: GridCoord
    fuel_remaining_pct: float = 1.0
    assigned_region_id: Optional[str] = None
    target_group_id: Optional[str] = None
    time_to_available: float = 0.0  # minutes until refueled/ready
    heading_deg: float = 0.0
    sensor_mode: str = "off"


@dataclass
class Marker:
    id: str
    position: GridCoord
    created_time: float
    source_uav_id: str


@dataclass
class TargetReport:
    """A target position that was actually observed by a UAV sensor.

    This object deliberately contains no reference to the environment's
    ground-truth ship instance.  Scheduling and LLM prompts may use only this
    report after a contact has been established.
    """
    group_id: str
    position: GridCoord
    observed_at: float
    source_uav_id: str
    velocity_cells_per_min: tuple[float, float] = (0.0, 0.0)
    observation_count: int = 1


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
