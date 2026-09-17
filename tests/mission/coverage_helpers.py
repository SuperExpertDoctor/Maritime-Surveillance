"""Independent persistent-coverage oracle and real-motion test rig."""

from dataclasses import dataclass
import math

import numpy as np

from src.control.common.contracts import (
    ActionSpec,
    BaseObservation,
    ControlMode,
    ControlOwner,
    ControlCommand,
    OperationMode,
    SensorMode,
)
from src.control.common.executor import UAVDynamicsExecutor
from src.control.common.observation import ObservationProvider
from src.control.common.safety import SafetyEnvelope
from src.env.sar_sensor import SARSensor
from src.env.uav_entity import UAVEntity
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox, GridCoord
from src.schedule.state_manager import StateManager


def oracle_coverage(events, fixed_cells, *, now_min, window_min, cell_size_km):
    domain = set(map(tuple, fixed_cells))
    recent = set()
    for event in events:
        if event["source"] == "sar" and now_min - window_min < event["time"] <= now_min:
            recent.update(map(tuple, event["cells"]))
    covered = recent & domain
    return {
        "covered_cells": len(covered),
        "covered_area_km2": len(covered) * cell_size_km**2,
        "coverage_pct": 100 * len(covered) / len(domain) if domain else None,
    }


class _RigController:
    phase = "scan"

    def __init__(self, speed_cells_min: float):
        self._speed_cells_min = speed_cells_min

    def act(self, _observation) -> ControlCommand:
        return ControlCommand(
            turn_rate_rad_min=0.0,
            speed_cells_min=self._speed_cells_min,
            sensor_mode=SensorMode.SAR,
            operation_mode=OperationMode.COVERAGE,
        )


@dataclass
class CoverageRig:
    controller: _RigController
    entity: UAVEntity
    observations: ObservationProvider
    safety: SafetyEnvelope
    executor: UAVDynamicsExecutor
    state: StateManager
    bbox: BBox
    dt_min: float
    current_time: float = 0.0
    _progress_cells: float = 0.0
    _max_speed_cells_min: float = 0.0

    def tick(self) -> dict:
        before = self.entity.pose
        observation = self.observations.build(
            self.entity,
            self.state,
            events=(),
            bases=(),
            control_mode=ControlMode.HEURISTIC,
            control_owner=ControlOwner.HEURISTIC,
            operation_mode=OperationMode.COVERAGE,
            safety_intervened=False,
            current_time=self.current_time,
            dt_min=self.dt_min,
        )
        requested = self.controller.act(observation)
        safe = self.safety.apply(requested, observation, self.dt_min)
        result = self.executor.execute(self.entity, safe, self.dt_min)
        footprint = []
        if result.applied_command.sensor_mode is SensorMode.SAR:
            footprint = [
                [cell.col, cell.row]
                for cell in self.entity.sar_sensor.compute_swath_footprint(
                    self.entity.float_position,
                    self.entity.heading_rad,
                    "right",
                    self.entity.sar_along_track_cells,
                )
            ]
        in_bbox = {
            (col, row)
            for col, row in footprint
            if self.bbox.col_start <= col < self.bbox.col_end
            and self.bbox.row_start <= row < self.bbox.row_end
        }
        self._progress_cells += len(in_bbox)
        self._max_speed_cells_min = max(
            self._max_speed_cells_min, result.applied_command.speed_cells_min
        )
        obstacle_intersection = any(
            self.state.obstacle_mask[col, row]
            for col, row in footprint
            if 0 <= col < self.state.obstacle_mask.shape[0]
            and 0 <= row < self.state.obstacle_mask.shape[1]
        )
        record = {
            "before_pose": before,
            "after_pose": self.entity.pose,
            "phase": self.controller.phase,
            "applied_command": result.applied_command,
            "footprint": footprint,
            "progress_cells": self._progress_cells,
            "sar_imaging": self.entity.sar_imaging,
            "distance_cells": result.distance_cells,
            "max_speed_cells_min": self._max_speed_cells_min,
            "obstacle_intersection": obstacle_intersection,
        }
        self.current_time += self.dt_min
        self.state.current_time = self.current_time
        return record

    def run(self, max_minutes: float) -> list[dict]:
        if max_minutes < 0 or not math.isfinite(max_minutes):
            raise ValueError("max_minutes must be finite and non-negative")
        records = []
        while self.current_time < max_minutes:
            records.append(self.tick())
        return records


def make_coverage_rig(*, bbox, start_pose, swath_cells=1.5, dt_min=1.0):
    bbox = BBox(*bbox)
    if dt_min not in (1.0, 0.25):
        raise ValueError("dt_min must be 1.0 or 0.25")
    config = ConfigLoader.load()
    config.grid.resolution = (30, 30)
    config.grid.cell_size_km = 10
    state = StateManager(config)
    state.episode_id = "coverage-rig"
    state.set_environment_obstacles([], np.zeros((30, 30), dtype=bool))
    state.set_land_mask(np.zeros((30, 30), dtype=bool))
    entity = UAVEntity(
        "coverage-rig-uav",
        GridCoord(int(start_pose[0]), int(start_pose[1])),
        endurance_h=1.0,
        cruise_speed_kmh=160.0,
        cell_size_km=10.0,
        R_min=1.0,
    )
    entity._col, entity._row, entity.heading_rad = map(float, start_pose)
    entity.sar_sensor = SARSensor(
        swath_width_cells=swath_cells,
        near_range_cells=0.25,
        grid_shape=(30, 30),
    )
    speed = 160.0 / 10.0 / 60.0
    return CoverageRig(
        controller=_RigController(speed),
        entity=entity,
        observations=ObservationProvider(config),
        safety=SafetyEnvelope(ActionSpec(-speed, speed, speed, speed)),
        executor=UAVDynamicsExecutor(),
        state=state,
        bbox=bbox,
        dt_min=dt_min,
    )
