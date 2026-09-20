"""Independent persistent-coverage oracle and real-motion test rig."""

from dataclasses import dataclass, replace
import math

import numpy as np

from src.control.common.contracts import (
    ActionSpec,
    ControlMode,
    ControlOwner,
    ObservationSpec,
    ControlTask,
    OperationMode,
    SensorMode,
)
from src.control.heuristic.coverage import CoverageController
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


@dataclass
class CoverageRig:
    controller: CoverageController
    entity: UAVEntity
    observations: ObservationProvider
    safety: SafetyEnvelope
    executor: UAVDynamicsExecutor
    state: StateManager
    bbox: BBox
    dt_min: float
    current_time: float = 0.0
    _progress_cells: float = 0.0

    def tick(self, *, inject_safety_obstacle: bool = False) -> dict:
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
        requested = self.controller.act(observation).command
        safety_obstacle_mask_cells = []
        safety_observation = observation
        if inject_safety_obstacle:
            requested_cells = self._command_cells(observation, requested)
            clipped = replace(
                requested,
                turn_rate_rad_min=self.safety._clip(
                    requested.turn_rate_rad_min,
                    self.safety._action_spec.min_turn_rate_rad_min,
                    self.safety._action_spec.max_turn_rate_rad_min,
                ),
                speed_cells_min=self.safety._clip(
                    requested.speed_cells_min,
                    self.safety._action_spec.min_speed_cells_min,
                    self.safety._action_spec.max_speed_cells_min,
                ),
            )
            alternatives = [clipped]
            for turn_rate in (
                self.safety._action_spec.max_turn_rate_rad_min,
                self.safety._action_spec.min_turn_rate_rad_min,
                0.0,
            ):
                alternatives.append(
                    replace(
                        requested,
                        turn_rate_rad_min=turn_rate,
                        speed_cells_min=self.safety._action_spec.min_speed_cells_min,
                    )
                )
            current_cell = (
                math.floor(observation.self_state.position[0]),
                math.floor(observation.self_state.position[1]),
            )
            for col, row in requested_cells:
                if (col, row) == current_cell:
                    continue
                if not (0 <= col < observation.planning_obstacle_mask.shape[0]):
                    continue
                if not (0 <= row < observation.planning_obstacle_mask.shape[1]):
                    continue
                mask = np.array(observation.planning_obstacle_mask, copy=True)
                mask[col, row] = True
                candidate_observation = replace(
                    observation, planning_obstacle_mask=mask
                )
                if self.safety._motion_blocked(
                    clipped, candidate_observation, self.dt_min
                ) and any(
                    not self.safety._motion_blocked(
                        alternative, candidate_observation, self.dt_min
                    )
                    for alternative in alternatives[1:]
                ):
                    safety_observation = candidate_observation
                    safety_obstacle_mask_cells.append([col, row])
                    break
        safe = self.safety.apply(requested, safety_observation, self.dt_min)
        result = self.executor.execute(self.entity, safe, self.dt_min)
        footprint = []
        if (
            result.applied_command.sensor_mode is SensorMode.SAR
            and self.entity.sar_imaging
        ):
            footprint = [
                [cell.col, cell.row]
                for cell in self.entity.sar_sensor.compute_swath_footprint(
                    self.entity.float_position,
                    self.entity.heading_rad,
                    result.applied_command.sar_look_direction,
                    self.entity.sar_along_track_cells,
                )
            ]
        in_bbox = {
            (col, row)
            for col, row in footprint
            if self.bbox.col_start <= col < self.bbox.col_end
            and self.bbox.row_start <= row < self.bbox.row_end
        }
        if self.controller.follower is not None:
            self._progress_cells = self.controller.follower.progress_cells
        obstacle_intersection = any(
            self.state.obstacle_mask[col, row]
            for col, row in footprint
            if 0 <= col < self.state.obstacle_mask.shape[0]
            and 0 <= row < self.state.obstacle_mask.shape[1]
        )
        record = {
            "before_pose": before,
            "after_pose": self.entity.pose,
            "phase": self.controller.phase.value,
            "applied_command": result.applied_command,
            "footprint": footprint,
            "progress_cells": self._progress_cells,
            "sar_imaging": self.entity.sar_imaging,
            "distance_cells": result.distance_cells,
            "max_speed_cells_min": result.applied_command.speed_cells_min,
            "obstacle_intersection": obstacle_intersection,
            "safety_intervened": bool(safe.interventions),
            "safety_interventions": safe.interventions,
            "safety_obstacle_mask_cells": safety_obstacle_mask_cells,
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
            if self.controller.follower is not None and self.controller.follower.is_complete:
                break
        return records

    def _command_cells(self, observation, command):
        mid_heading = (
            observation.self_state.heading_rad
            + command.turn_rate_rad_min * self.dt_min / 2.0
        )
        distance = command.speed_cells_min * self.dt_min
        end_col = observation.self_state.position[0] + distance * math.cos(mid_heading)
        end_row = observation.self_state.position[1] + distance * math.sin(mid_heading)
        return SafetyEnvelope._traversed_cells(
            observation.self_state.position[0],
            observation.self_state.position[1],
            end_col,
            end_row,
        )


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
    action_spec = ActionSpec(-speed, speed, speed, speed)
    observation_provider = ObservationProvider(config)
    observation = observation_provider.build(
        entity,
        state,
        events=(),
        bases=(),
        control_mode=ControlMode.HEURISTIC,
        control_owner=ControlOwner.HEURISTIC,
        operation_mode=OperationMode.COVERAGE,
        safety_intervened=False,
        current_time=0.0,
        dt_min=dt_min,
    )
    controller = CoverageController(
        observation_spec=controller_observation_spec(config),
        action_spec=action_spec,
        swath_width=swath_cells,
        r_min=entity.R_min,
        sar_along_track_cells=entity.sar_along_track_cells,
    )
    controller.start_task(
        ControlTask("coverage-rig-task", OperationMode.COVERAGE, bbox), observation
    )
    return CoverageRig(
        controller=controller,
        entity=entity,
        observations=observation_provider,
        safety=SafetyEnvelope(action_spec),
        executor=UAVDynamicsExecutor(controller.coverage_execution),
        state=state,
        bbox=bbox,
        dt_min=dt_min,
    )


def controller_observation_spec(config):
    return ObservationSpec(
        config.control.observation.schema_version,
        config.control.observation.local_window_cells,
    )
