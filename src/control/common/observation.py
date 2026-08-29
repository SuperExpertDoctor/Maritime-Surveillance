"""Truth-isolated control-observation snapshots."""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

import numpy as np

from src.control.common.contracts import (
    ActionMask,
    BaseObservation,
    ContactObservation,
    ControlEvent,
    ControlMode,
    ControlObservation,
    ControlOwner,
    HazardObservation,
    OperationMode,
    SensorMode,
    UAVObservation,
)
from src.env.obstacle import Island, Thunderstorm
from src.env.uav_entity import UAVEntity
from src.schedule.config_loader import AppConfig
from src.schedule.state_manager import StateManager


_OPERATION_BY_STATUS = {
    "idle": OperationMode.IDLE,
    "transit": OperationMode.TRANSIT,
    "searching": OperationMode.COVERAGE,
    "tracking": OperationMode.TRACK,
    "returning": OperationMode.RETURN,
    "holding": OperationMode.HOLDING,
    "refueling": OperationMode.IDLE,
}


class ObservationProvider:
    """Build immutable controller inputs from published scheduler state."""

    def __init__(self, config: AppConfig):
        self._config = config

    def build(
        self,
        entity: UAVEntity,
        state_manager: StateManager,
        *,
        events: Sequence[ControlEvent],
        bases: Sequence[BaseObservation],
        control_mode: ControlMode,
        control_owner: ControlOwner,
        operation_mode: OperationMode,
        safety_intervened: bool,
        current_time: float,
        dt_min: float,
    ) -> ControlObservation:
        """Publish the controller's complete, immutable observation boundary."""
        window_cells = self._config.control.observation.local_window_cells
        center = tuple(int(round(value)) for value in entity.float_position)
        local_info = self._window(state_manager.get_info_matrix(), center, window_cells, 0.0)
        local_value = self._window(state_manager.get_value_matrix(), center, window_cells, 0.0)
        local_obstacles = self._window(
            state_manager.obstacle_mask, center, window_cells, True
        )
        local_searchable = self._window(
            state_manager.get_searchable_mask(), center, window_cells, False
        )
        contacts = self._contacts(state_manager, current_time)
        return ControlObservation(
            schema_version=self._config.control.observation.schema_version,
            timestamp_min=float(current_time),
            dt_min=float(dt_min),
            self_state=self._self_state(
                entity,
                control_mode,
                control_owner,
                operation_mode,
                safety_intervened,
            ),
            local_info=np.asarray(local_info, dtype=np.float32),
            local_value=np.asarray(local_value, dtype=np.float32),
            obstacle_mask=np.asarray(local_obstacles, dtype=np.bool_),
            searchable_mask=np.asarray(local_searchable, dtype=np.bool_),
            planning_obstacle_mask=np.array(
                state_manager.obstacle_mask, dtype=np.bool_, copy=True
            ),
            planning_map_version=state_manager.obstacle_version,
            contacts=contacts,
            hazards=self._hazards(state_manager.obstacles),
            bases=tuple(sorted(bases, key=lambda base: base.base_id)),
            shared_uavs=self._shared_uavs(state_manager),
            events=tuple(sorted(events, key=lambda event: event.sequence)),
            action_mask=self._action_mask(control_owner, operation_mode, contacts),
        )

    @staticmethod
    def _window(
        source: np.ndarray,
        center: tuple[int, int],
        window_cells: int,
        padding: float | bool,
    ) -> np.ndarray:
        result = np.full((window_cells, window_cells), padding, dtype=source.dtype)
        half = window_cells // 2
        start_col, start_row = center[0] - half, center[1] - half
        end_col, end_row = start_col + window_cells, start_row + window_cells
        source_col_start, source_row_start = max(0, start_col), max(0, start_row)
        source_col_end = min(source.shape[0], end_col)
        source_row_end = min(source.shape[1], end_row)
        if source_col_start >= source_col_end or source_row_start >= source_row_end:
            return result
        target_col_start, target_row_start = (
            source_col_start - start_col,
            source_row_start - start_row,
        )
        target_col_end = target_col_start + source_col_end - source_col_start
        target_row_end = target_row_start + source_row_end - source_row_start
        result[
            target_col_start:target_col_end, target_row_start:target_row_end
        ] = source[source_col_start:source_col_end, source_row_start:source_row_end]
        return result

    @staticmethod
    def _self_state(
        entity: UAVEntity,
        control_mode: ControlMode,
        control_owner: ControlOwner,
        operation_mode: OperationMode,
        safety_intervened: bool,
    ) -> UAVObservation:
        return UAVObservation(
            uav_id=entity.id,
            position=tuple(float(value) for value in entity.float_position),
            heading_rad=float(entity.heading_rad),
            speed_cells_min=entity.cruise_speed_kmh / 60.0 / entity.cell_size_km,
            remaining_range_cells=float(entity.remaining_range_cells),
            control_mode=control_mode,
            control_owner=control_owner,
            operation_mode=operation_mode,
            sensor_mode=SensorMode(entity.sensor_mode),
            safety_intervened=bool(safety_intervened),
        )

    @staticmethod
    def _contacts(
        state_manager: StateManager, current_time: float
    ) -> tuple[ContactObservation, ...]:
        return tuple(
            ContactObservation(
                contact_id=report.group_id,
                group_id=report.group_id,
                estimated_position=(
                    float(report.position.col),
                    float(report.position.row),
                ),
                estimated_velocity=tuple(
                    float(value) for value in report.velocity_cells_per_min
                ),
                source=report.source_uav_id,
                observed_at_min=float(report.observed_at),
                age_min=max(0.0, float(current_time) - report.observed_at),
                confidence=1.0,
            )
            for report in sorted(
                state_manager.get_target_reports(), key=lambda report: report.group_id
            )
        )

    @staticmethod
    def _hazards(obstacles: Iterable[object]) -> tuple[HazardObservation, ...]:
        snapshots = []
        for obstacle in obstacles:
            if isinstance(obstacle, Island):
                snapshots.append(
                    HazardObservation(
                        hazard_id=obstacle.id,
                        hazard_type="island",
                        center=obstacle.center,
                        half_extent_cells=obstacle.half_extent,
                        velocity_cells_min=(0.0, 0.0),
                        intensity=1.0,
                    )
                )
            elif isinstance(obstacle, Thunderstorm):
                snapshots.append(
                    HazardObservation(
                        hazard_id=obstacle.id,
                        hazard_type="thunderstorm",
                        center=obstacle.center,
                        half_extent_cells=obstacle.half_extent,
                        velocity_cells_min=obstacle.move_vector,
                        intensity=obstacle.intensity,
                    )
                )
        return tuple(sorted(snapshots, key=lambda hazard: hazard.hazard_id))

    def _shared_uavs(self, state_manager: StateManager) -> tuple[UAVObservation, ...]:
        speed = self._config.uav.cruise_speed_kmh / 60.0 / self._config.grid.cell_size_km
        range_cells = (
            self._config.uav.sortie_endurance_h
            * self._config.uav.cruise_speed_kmh
            / self._config.grid.cell_size_km
        )
        snapshots = []
        for uav in state_manager.get_all_uavs():
            snapshots.append(
                UAVObservation(
                    uav_id=uav.id,
                    position=(float(uav.position.col), float(uav.position.row)),
                    heading_rad=math.radians(uav.heading_deg),
                    speed_cells_min=speed,
                    remaining_range_cells=uav.fuel_remaining_pct * range_cells,
                    control_mode=ControlMode(
                        self._config.control.per_uav.get(
                            uav.id, self._config.control.default_mode
                        )
                    ),
                    control_owner=ControlOwner.SYSTEM,
                    operation_mode=_OPERATION_BY_STATUS[uav.status],
                    sensor_mode=SensorMode(uav.sensor_mode),
                    safety_intervened=False,
                )
            )
        return tuple(sorted(snapshots, key=lambda uav: uav.uav_id))

    @staticmethod
    def _action_mask(
        control_owner: ControlOwner,
        operation_mode: OperationMode,
        contacts: Sequence[ContactObservation],
    ) -> ActionMask:
        target_contact_ids = tuple(sorted(contact.contact_id for contact in contacts))
        if control_owner in (ControlOwner.HEURISTIC, ControlOwner.LEARNING):
            sensor_modes = [SensorMode.OFF, SensorMode.SAR]
            operation_modes = [OperationMode.TRANSIT, OperationMode.COVERAGE]
            if target_contact_ids:
                sensor_modes.append(SensorMode.EO)
                operation_modes.append(OperationMode.TRACK)
        elif control_owner is ControlOwner.SYSTEM and operation_mode is OperationMode.RETURN:
            sensor_modes = [SensorMode.OFF]
            operation_modes = [OperationMode.RETURN, OperationMode.HOLDING]
        else:
            sensor_modes = [SensorMode.OFF]
            operation_modes = [OperationMode.IDLE]
        return ActionMask(tuple(sensor_modes), tuple(operation_modes), target_contact_ids)
