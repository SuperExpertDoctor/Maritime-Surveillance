import math

import numpy as np
import pytest

from src.control.common.contracts import (
    ActionMask,
    ActionSpec,
    ControlCommand,
    ControlMode,
    ControlObservation,
    ControlOwner,
    OperationMode,
    SensorMode,
    UAVObservation,
)
from src.control.common.safety import (
    InvalidControlCommand,
    SafetyEnvelope,
    UnsafeControlState,
)


@pytest.fixture
def setup():
    action_spec = ActionSpec(-0.2, 0.2, 0.1, 0.5)
    envelope = SafetyEnvelope(action_spec)
    observation = make_observation(
        position=(1.5, 2.5),
        action_mask=ActionMask(
            (SensorMode.OFF, SensorMode.SAR, SensorMode.EO),
            (OperationMode.TRANSIT, OperationMode.COVERAGE, OperationMode.TRACK),
            ("G1",),
        ),
    )
    return envelope, observation


def make_observation(
    *,
    position=(1.5, 2.5),
    heading_rad=0.0,
    obstacle_mask=None,
    action_mask=None,
):
    obstacle_mask = (
        np.zeros((5, 5), dtype=bool) if obstacle_mask is None else obstacle_mask
    )
    action_mask = action_mask or ActionMask(
        (SensorMode.OFF, SensorMode.SAR, SensorMode.EO),
        (OperationMode.TRANSIT, OperationMode.COVERAGE, OperationMode.TRACK),
        ("G1",),
    )
    return ControlObservation(
        schema_version="control-observation/v1",
        timestamp_min=0.0,
        dt_min=1.0,
        self_state=UAVObservation(
            uav_id="UAV-1",
            position=position,
            heading_rad=heading_rad,
            speed_cells_min=0.25,
            remaining_range_cells=10.0,
            control_mode=ControlMode.HEURISTIC,
            control_owner=ControlOwner.HEURISTIC,
            operation_mode=OperationMode.TRANSIT,
            sensor_mode=SensorMode.OFF,
            safety_intervened=False,
        ),
        local_info=np.zeros((1, 1), dtype=np.float32),
        local_value=np.zeros((1, 1), dtype=np.float32),
        obstacle_mask=np.zeros((1, 1), dtype=bool),
        searchable_mask=np.ones((1, 1), dtype=bool),
        planning_obstacle_mask=obstacle_mask,
        planning_map_version=1,
        contacts=(),
        hazards=(),
        bases=(),
        shared_uavs=(),
        events=(),
        action_mask=action_mask,
    )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_safety_rejects_non_finite_continuous_actions(setup, value):
    envelope, observation = setup
    command = ControlCommand(
        turn_rate_rad_min=value,
        speed_cells_min=0.25,
        sensor_mode=SensorMode.OFF,
        operation_mode=OperationMode.TRANSIT,
    )

    with pytest.raises(InvalidControlCommand, match="finite"):
        envelope.apply(command, observation, dt_min=1.0)


def test_safety_clips_turn_and_speed_to_action_spec(setup):
    envelope, observation = setup
    command = ControlCommand(
        turn_rate_rad_min=1.0,
        speed_cells_min=1.0,
        sensor_mode=SensorMode.OFF,
        operation_mode=OperationMode.TRANSIT,
    )

    result = envelope.apply(command, observation, dt_min=1.0)

    assert result.applied_command.turn_rate_rad_min == pytest.approx(0.2)
    assert result.applied_command.speed_cells_min == pytest.approx(0.5)
    assert {item.kind for item in result.interventions} == {
        "turn_rate_clipped",
        "speed_clipped",
    }


def test_safety_preserves_task_intent_while_avoiding_blocked_boundary_step(setup):
    envelope, _ = setup
    observation = make_observation(position=(4.4, 2.5))
    requested = ControlCommand(
        turn_rate_rad_min=10.0,
        speed_cells_min=10.0,
        sensor_mode=SensorMode.EO,
        operation_mode=OperationMode.TRACK,
        target_contact_id="G1",
    )

    result = envelope.apply(requested, observation, dt_min=1.0)

    assert result.applied_command.operation_mode is OperationMode.TRACK
    assert result.applied_command.target_contact_id == "G1"
    assert result.applied_command.speed_cells_min == pytest.approx(0.1)
    assert result.interventions


def test_safety_avoids_predicted_obstacle_collision(setup):
    envelope, observation = setup
    obstacle_mask = observation.planning_obstacle_mask.copy()
    obstacle_mask[2, 2] = True
    observation = make_observation(obstacle_mask=obstacle_mask)
    command = ControlCommand(
        turn_rate_rad_min=0.0,
        speed_cells_min=0.5,
        sensor_mode=SensorMode.OFF,
        operation_mode=OperationMode.TRANSIT,
    )

    result = envelope.apply(command, observation, dt_min=1.0)

    assert result.applied_command.speed_cells_min == pytest.approx(0.1)
    assert "motion_corrected" in {item.kind for item in result.interventions}


def test_safety_masks_sar_outside_stable_coverage_leg(setup):
    envelope, observation = setup
    command = ControlCommand(
        turn_rate_rad_min=0.2,
        speed_cells_min=0.25,
        sensor_mode=SensorMode.SAR,
        operation_mode=OperationMode.COVERAGE,
        sar_look_direction="right",
        sar_scan_heading_rad=0.0,
        sar_scan_origin=(1.5, 2.5),
    )

    result = envelope.apply(command, observation, dt_min=1.0)

    assert result.applied_command.sensor_mode is SensorMode.OFF
    assert result.applied_command.operation_mode is OperationMode.COVERAGE
    assert "sensor_mode_masked" in {item.kind for item in result.interventions}


def test_safety_rejects_masked_sar_before_stability_masking(setup):
    envelope, observation = setup
    observation = make_observation(
        action_mask=ActionMask(
            (SensorMode.OFF, SensorMode.EO),
            (OperationMode.TRANSIT, OperationMode.COVERAGE, OperationMode.TRACK),
            ("G1",),
        )
    )
    command = ControlCommand(
        turn_rate_rad_min=0.2,
        speed_cells_min=0.25,
        sensor_mode=SensorMode.SAR,
        operation_mode=OperationMode.TRANSIT,
        sar_look_direction="right",
        sar_scan_heading_rad=0.0,
        sar_scan_origin=(1.5, 2.5),
    )

    with pytest.raises(InvalidControlCommand, match="sensor mode"):
        envelope.apply(command, observation, dt_min=1.0)


def test_safety_blocks_a_long_diagonal_trajectory_that_cuts_a_blocked_corner(setup):
    envelope = SafetyEnvelope(ActionSpec(-0.2, 0.2, 0.1, 3.0))
    obstacle_mask = np.zeros((5, 5), dtype=bool)
    obstacle_mask[2, 1] = True
    observation = make_observation(
        position=(1.5, 1.5),
        heading_rad=math.pi / 4.0,
        obstacle_mask=obstacle_mask,
    )
    command = ControlCommand(
        turn_rate_rad_min=0.0,
        speed_cells_min=math.sqrt(8.0),
        sensor_mode=SensorMode.OFF,
        operation_mode=OperationMode.TRANSIT,
    )

    result = envelope.apply(command, observation, dt_min=1.0)

    assert result.applied_command.speed_cells_min == pytest.approx(0.1)
    assert "motion_corrected" in {item.kind for item in result.interventions}


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            (0.5, 0.5),
            (2.0, 2.0),
            (
                (0, 0),
                (1, 0),
                (0, 1),
                (1, 1),
                (2, 1),
                (1, 2),
                (2, 2),
            ),
        ),
        (
            (10.0, 12.0),
            (8.0, 13.0),
            ((10, 12), (9, 12), (8, 12), (8, 13)),
        ),
        (
            (2.5, 2.5),
            (0.0, 0.0),
            (
                (2, 2),
                (1, 2),
                (2, 1),
                (1, 1),
                (0, 1),
                (1, 0),
                (0, 0),
            ),
        ),
    ],
    ids=["positive-terminal-corner", "mixed-sign", "negative-terminal-corner"],
)
def test_safety_diagonal_supercover_is_complete_and_direction_symmetric(
    start, end, expected
):
    forward = SafetyEnvelope._traversed_cells(*start, *end)
    reverse = SafetyEnvelope._traversed_cells(*end, *start)

    assert forward == expected
    assert reverse == tuple(reversed(expected))


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ((0.0, 1.0), (3.0, 1.0), ((0, 1), (1, 1), (2, 1), (3, 1))),
        ((1.0, 0.0), (1.0, 3.0), ((1, 0), (1, 1), (1, 2), (1, 3))),
        ((0.5, 1.5), (2.0, 1.5), ((0, 1), (1, 1), (2, 1))),
        ((1.5, 0.5), (1.5, 2.0), ((1, 0), (1, 1), (1, 2))),
    ],
    ids=["horizontal", "vertical", "horizontal-end-boundary", "vertical-end-boundary"],
)
def test_safety_axis_aligned_supercover_respects_exact_boundaries(
    start, end, expected
):
    forward = SafetyEnvelope._traversed_cells(*start, *end)
    reverse = SafetyEnvelope._traversed_cells(*end, *start)

    assert forward == expected
    assert reverse == tuple(reversed(expected))


@pytest.mark.parametrize(
    ("start", "heading"),
    [
        ((0.5, 0.5), math.pi / 4.0),
        ((2.0, 2.0), -3.0 * math.pi / 4.0),
    ],
    ids=["forward", "reverse"],
)
def test_safety_blocks_a_side_cell_touched_at_a_terminal_corner(start, heading):
    obstacle_mask = np.zeros((4, 4), dtype=bool)
    obstacle_mask[2, 1] = True
    observation = make_observation(
        position=start,
        heading_rad=heading,
        obstacle_mask=obstacle_mask,
    )
    command = ControlCommand(
        turn_rate_rad_min=0.0,
        speed_cells_min=math.hypot(1.5, 1.5),
        sensor_mode=SensorMode.OFF,
        operation_mode=OperationMode.TRANSIT,
    )

    assert SafetyEnvelope._motion_blocked(command, observation, dt_min=1.0)


def test_safety_blocks_a_negative_direction_diagonal_at_its_endpoint():
    obstacle_mask = np.zeros((16, 16), dtype=bool)
    obstacle_mask[8, 13] = True
    observation = make_observation(
        position=(10.0, 12.0),
        heading_rad=math.atan2(1.0, -2.0),
        obstacle_mask=obstacle_mask,
    )
    command = ControlCommand(
        turn_rate_rad_min=0.0,
        speed_cells_min=math.sqrt(5.0),
        sensor_mode=SensorMode.OFF,
        operation_mode=OperationMode.TRANSIT,
    )

    assert SafetyEnvelope._motion_blocked(command, observation, dt_min=1.0)


def test_safety_rejects_masked_operation_or_unknown_target(setup):
    envelope, observation = setup
    operation = ControlCommand(0.0, 0.25, SensorMode.OFF, OperationMode.RETURN)
    unknown_target = ControlCommand(
        0.0, 0.25, SensorMode.EO, OperationMode.TRACK, "missing"
    )

    with pytest.raises(InvalidControlCommand, match="operation"):
        envelope.apply(operation, observation, dt_min=1.0)
    with pytest.raises(InvalidControlCommand, match="target"):
        envelope.apply(unknown_target, observation, dt_min=1.0)


def test_safety_rejects_sar_without_complete_geometry(setup):
    envelope, observation = setup
    command = ControlCommand(
        0.0,
        0.25,
        SensorMode.SAR,
        OperationMode.COVERAGE,
    )

    with pytest.raises(InvalidControlCommand, match="SAR geometry"):
        envelope.apply(command, observation, dt_min=1.0)


def test_safety_clears_geometry_when_sar_is_masked_or_off(setup):
    envelope, observation = setup
    command = ControlCommand(
        0.0,
        0.25,
        SensorMode.OFF,
        OperationMode.TRANSIT,
        sar_look_direction="right",
        sar_scan_heading_rad=0.0,
        sar_scan_origin=(1.5, 2.5),
    )

    result = envelope.apply(command, observation, dt_min=1.0)

    assert result.applied_command.sar_look_direction is None
    assert result.applied_command.sar_scan_heading_rad is None
    assert result.applied_command.sar_scan_origin is None


@pytest.mark.parametrize(
    "field_overrides",
    [
        {"sar_look_direction": "forward"},
        {"sar_scan_heading_rad": math.inf},
        {"sar_scan_origin": (math.nan, 2.5)},
        {"sar_scan_origin": (1.5,)},
        {"sar_scan_origin": ("x", 2.5)},
    ],
)
def test_safety_rejects_invalid_sar_geometry(setup, field_overrides):
    envelope, observation = setup
    fields = {
        "sar_look_direction": "right",
        "sar_scan_heading_rad": 0.0,
        "sar_scan_origin": (1.5, 2.5),
    }
    fields.update(field_overrides)
    command = ControlCommand(
        0.0,
        0.25,
        SensorMode.SAR,
        OperationMode.COVERAGE,
        **fields,
    )

    with pytest.raises(InvalidControlCommand, match="SAR geometry"):
        envelope.apply(command, observation, dt_min=1.0)


def test_safety_rejects_unknown_command_schema(setup):
    envelope, observation = setup
    command = ControlCommand(
        0.0,
        0.25,
        SensorMode.OFF,
        OperationMode.TRANSIT,
        schema_version="control-command/v0",
    )

    with pytest.raises(InvalidControlCommand, match="schema"):
        envelope.apply(command, observation, dt_min=1.0)


def test_safety_raises_when_no_legal_collision_free_candidate_exists(setup):
    envelope, observation = setup
    obstacle_mask = observation.planning_obstacle_mask.copy()
    obstacle_mask[1, 2] = True
    observation = make_observation(obstacle_mask=obstacle_mask)
    command = ControlCommand(0.0, 0.5, SensorMode.OFF, OperationMode.TRANSIT)

    with pytest.raises(UnsafeControlState):
        envelope.apply(command, observation, dt_min=1.0)


def _replay_aircraft(position, heading):
    from src.env.uav_entity import UAVEntity
    from src.schedule.datatypes import GridCoord

    uav = UAVEntity("replay", GridCoord(0, 0), endurance_h=8.0, cruise_speed_kmh=160.0)
    uav._col, uav._row = position
    uav.heading_rad = math.radians(heading)
    return uav


@pytest.mark.parametrize(
    "position,heading,target",
    [
        ((0.8870955716, 13.6736357057), 181.887336, 225.0),
        ((6.9875285876, 0.7574690928), 251.997988, 270.0),
        ((0.6537893655, 24.8952793555), 176.242190, 180.0),
    ],
)
def test_live_edge_approaches_keep_room_to_turn(position, heading, target):
    from dataclasses import replace

    spec = ActionSpec(-0.32, 0.32, 0.16, 0.32)
    envelope = SafetyEnvelope(spec)
    observation = make_observation(
        position=position,
        heading_rad=math.radians(heading),
        obstacle_mask=np.zeros((30, 30), dtype=bool),
    )
    uav = _replay_aircraft(position, heading)
    corrected = 0
    for _ in range(60):
        state = observation.self_state
        error = (math.radians(target) - state.heading_rad + math.pi) % (
            2 * math.pi
        ) - math.pi
        command = ControlCommand(
            error, 0.2666666667, SensorMode.OFF, OperationMode.TRANSIT
        )
        result = envelope.apply(command, observation, 1.0)
        corrected += any(i.kind == "motion_corrected" for i in result.interventions)
        applied = result.applied_command
        uav.apply_motion(applied.turn_rate_rad_min, applied.speed_cells_min, 1.0)
        xy = uav.float_position
        assert all(0 <= value < 30 for value in xy)
        observation = replace(
            observation,
            self_state=replace(state, position=xy, heading_rad=uav.heading_rad),
        )
    assert corrected > 0


@pytest.mark.parametrize(
    "position,heading,center",
    [
        ((12.2, 13.3262032811), 270.0, (10.3306720092, 13.1754973621)),
        ((14.2593773053, 8.2), 180.0, (11.8468812469, 10.7573412845)),
    ],
)
def test_live_drifting_storm_approaches_do_not_strand_aircraft(
    position, heading, center
):
    from dataclasses import replace
    from src.control.common.contracts import HazardObservation
    from src.env.obstacle import Thunderstorm, obstacle_grid_mask

    storm = Thunderstorm(
        center=center, size=2, move_vector=(0.0309430457, -0.049350124)
    )
    envelope = SafetyEnvelope(ActionSpec(-0.32, 0.32, 0.16, 0.32))
    observation = make_observation(
        position=position,
        heading_rad=math.radians(heading),
        obstacle_mask=obstacle_grid_mask([storm], (30, 30), 1.0),
    )
    uav = _replay_aircraft(position, heading)
    corrected = 0
    for _ in range(40):
        hazard = HazardObservation(
            storm.id,
            "thunderstorm",
            storm.center,
            storm.half_extent,
            storm.move_vector,
            storm.intensity,
            safety_margin_cells=1.0,
        )
        observation = replace(
            observation,
            planning_obstacle_mask=obstacle_grid_mask([storm], (30, 30), 1.0),
            hazards=(hazard,),
        )
        state = observation.self_state
        error = (math.radians(heading) - state.heading_rad + math.pi) % (
            2 * math.pi
        ) - math.pi
        result = envelope.apply(
            ControlCommand(error, 0.2666666667, SensorMode.OFF, OperationMode.TRANSIT),
            observation,
            1.0,
        )
        corrected += any(i.kind == "motion_corrected" for i in result.interventions)
        command = result.applied_command
        uav.apply_motion(command.turn_rate_rad_min, command.speed_cells_min, 1.0)
        xy = uav.float_position
        storm.step(1.0)
        mask = obstacle_grid_mask([storm], (30, 30), 1.0)
        assert not SafetyEnvelope._point_blocked(*xy, mask)
        observation = replace(
            observation,
            self_state=replace(state, position=xy, heading_rad=uav.heading_rad),
        )
    assert corrected > 0


def test_safety_rejects_boundary_pose_without_room_to_turn(setup):
    envelope, _ = setup
    observation = make_observation(position=(4.9, 2.5))
    command = ControlCommand(0.0, 0.1, SensorMode.OFF, OperationMode.TRANSIT)
    with pytest.raises(UnsafeControlState):
        envelope.apply(command, observation, 1.0)


def test_storm_forecast_matches_raster_margin_and_boundary_reflection():
    from dataclasses import replace
    from src.control.common.contracts import HazardObservation
    from src.env.obstacle import Thunderstorm, obstacle_grid_mask

    storm = Thunderstorm(center=(28.9, 1.1), size=2.0, move_vector=(0.2, -0.3))
    current = obstacle_grid_mask([storm], (30, 30), 1.0)
    observation = replace(
        make_observation(obstacle_mask=current),
        hazards=(
            HazardObservation(
                storm.id,
                "thunderstorm",
                storm.center,
                storm.half_extent,
                storm.move_vector,
                storm.intensity,
                safety_margin_cells=1.0,
            ),
        ),
    )
    envelope = SafetyEnvelope(ActionSpec(-0.32, 0.32, 0.16, 0.32))
    forecasts = envelope._forecast_masks(observation, 1.0)
    for forecast in forecasts[1:]:
        storm.step(1.0, (30, 30))
        expected = current | obstacle_grid_mask([storm], (30, 30), 1.0)
        np.testing.assert_array_equal(forecast, expected)
    np.testing.assert_array_equal(observation.planning_obstacle_mask, current)
