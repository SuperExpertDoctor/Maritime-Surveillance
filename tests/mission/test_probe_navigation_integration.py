from __future__ import annotations

import math

import numpy as np

from src.control.common.contracts import (
    ActionMask,
    ActionSpec,
    ContactObservation,
    ControlMode,
    ControlObservation,
    ControlOwner,
    ControlTask,
    ObservationSpec,
    OperationMode,
    SensorMode,
    UAVObservation,
)
from src.control.common.executor import UAVDynamicsExecutor
from src.control.heuristic.navigation import AStarNavigator
from src.control.heuristic.probe import ProbeController
from src.env.uav_entity import UAVEntity
from src.mission.contracts import ProbeSession
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import GridCoord


def _probe() -> ProbeSession:
    return ProbeSession(
        "P-INTEGRATION", "C-INTEGRATION", "UAV-1", "baseline", 0.0,
        None, 0.0, (), (), 0.0, None,
    )


def _observation(uav: UAVEntity, contact: ContactObservation) -> ControlObservation:
    arrays = np.zeros((40, 40), dtype=bool)
    return ControlObservation(
        schema_version="control-observation/v2",
        timestamp_min=0.0,
        dt_min=0.5,
        self_state=UAVObservation(
            "UAV-1",
            uav.float_position,
            uav.heading_rad,
            1.0,
            uav.remaining_range_cells,
            ControlMode.HEURISTIC,
            ControlOwner.HEURISTIC,
            OperationMode.PROBE,
            SensorMode.OFF,
            False,
        ),
        local_info=arrays,
        local_value=arrays,
        obstacle_mask=arrays,
        searchable_mask=np.ones((40, 40), dtype=bool),
        planning_obstacle_mask=arrays,
        planning_map_version=1,
        contacts=(contact,),
        hazards=(),
        bases=(),
        shared_uavs=(),
        events=(),
        action_mask=ActionMask(
            (SensorMode.OFF, SensorMode.EO),
            (OperationMode.PROBE, OperationMode.HOLDING),
            (contact.contact_id,),
        ),
        probe=_probe(),
    )


def test_probe_real_navigator_and_executor_reach_a_non_horizontal_contact():
    uav = UAVEntity(
        "UAV-1", GridCoord(2, 2), endurance_h=8.0, cruise_speed_kmh=160.0,
        cell_size_km=10.0,
    )
    uav.heading_rad = math.pi / 2.0
    contact = ContactObservation(
        "C-INTEGRATION", "G-INTEGRATION", (14.0, 17.0), (0.0, 0.0),
        "UAV-reporting", 0.0, 0.0, 1.0,
    )
    initial = _observation(uav, contact)
    controller = ProbeController(
        observation_spec=ObservationSpec("control-observation/v2", 11),
        action_spec=ActionSpec(-2.0, 2.0, 0.5, 1.0),
        contact_config=ConfigLoader.load().mission.contact,
        navigator=AStarNavigator(
            xy_resolution=0.5,
            heading_bins=72,
            candidate_limit=32,
            primitive_length=1.0,
            sample_step=0.2,
        ),
    )
    controller.start_task(
        ControlTask(
            "probe:C-INTEGRATION", OperationMode.PROBE,
            target_contact_id=contact.contact_id,
            probe_id="P-INTEGRATION",
        ),
        initial,
    )
    route = controller.route_snapshot()
    assert route is not None
    assert route.status == "ready"
    assert route.route

    executor = UAVDynamicsExecutor()
    initial_distance = math.dist(uav.float_position, contact.estimated_position)
    distances = [initial_distance]
    headings = [uav.heading_rad]
    for _ in range(240):
        observation = _observation(uav, contact)
        decision = controller.act(observation)
        executor.execute(uav, decision.command, observation.dt_min)
        position = uav.float_position
        assert all(math.isfinite(value) for value in (*position, uav.heading_rad))
        distances.append(math.dist(position, contact.estimated_position))
        headings.append(uav.heading_rad)
        if distances[-1] <= 1.95:
            break

    assert min(distances) < initial_distance
    assert min(distances) <= 1.95
    assert max(abs(heading - headings[0]) for heading in headings[1:]) > 0.05
