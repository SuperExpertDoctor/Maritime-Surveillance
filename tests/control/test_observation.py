import inspect

import numpy as np
import pytest

from src.control.common.contracts import (
    BaseObservation,
    ControlEvent,
    ControlMode,
    ControlOwner,
    OperationMode,
    SensorMode,
)
from src.control.common.observation import ObservationProvider
from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import GridCoord


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setenv("LONGCAT_API_KEY", "t06-offline-fixture")
    config = ConfigLoader.load()
    # Keep the original no-broadcast fixture; global AIS is tested separately.
    from dataclasses import replace
    config.ship = replace(config.ship, target_ship_count=config.ship.initial_ship_count,
                          target_ais_on_probability=0.0)
    return SimulationEngine(config, seed=17)


def make_provider(engine):
    return ObservationProvider(engine.config)


def make_base_observations(bases):
    return tuple(
        BaseObservation(
            base_id=base.id,
            position=(float(base.position.col), float(base.position.row)),
            capacity=base.capacity,
            reserved_load=base.occupancy,
        )
        for base in bases
    )


def build_observation(engine, **overrides):
    values = {
        "events": (),
        "bases": make_base_observations(engine.bases),
        "control_mode": ControlMode.HEURISTIC,
        "control_owner": ControlOwner.SYSTEM,
        "operation_mode": OperationMode.IDLE,
        "safety_intervened": False,
        "current_time": engine.clock.time,
        "dt_min": engine.clock.dt_min,
    }
    values.update(overrides)
    return make_provider(engine).build(engine.uavs[0], engine.allocator.sm, **values)


def test_observation_excludes_undetected_ship_truth(engine):
    hidden = engine.ships[0]
    hidden._col = 27.12345
    hidden._row = 26.54321
    hidden.actual_military = True

    observation = build_observation(engine)

    assert observation.contacts == ()
    assert "27.12345" not in repr(observation)
    assert "actual_military" not in repr(observation)
    assert "ships" not in inspect.signature(ObservationProvider).parameters
    assert "ships" not in inspect.signature(ObservationProvider.build).parameters


def test_observation_uses_read_only_local_window_and_ordered_events(engine):
    engine.uavs[0]._col = 0.0
    engine.uavs[0]._row = 0.0
    engine.allocator.sm.get_info_matrix()[0, 0] = 99.0
    events = (
        ControlEvent(2, 2.0, "second", "coordinator", "UAV-1", {}),
        ControlEvent(1, 1.0, "first", "coordinator", "UAV-1", {}),
    )

    observation = build_observation(engine, events=events)

    assert observation.local_info.shape == (11, 11)
    assert observation.local_info.dtype == np.float32
    assert not observation.local_info.flags.writeable
    assert [event.sequence for event in observation.events] == [1, 2]
    assert observation.local_info[0, 0] == 0.0
    assert observation.local_value[0, 0] == 0.0
    assert observation.obstacle_mask[0, 0]
    assert not observation.searchable_mask[0, 0]
    for array in (
        observation.local_value,
        observation.obstacle_mask,
        observation.searchable_mask,
        observation.planning_obstacle_mask,
    ):
        assert not array.flags.writeable


def test_observation_exposes_only_target_reports_and_published_hazards(engine):
    sm = engine.allocator.sm
    sm.record_target_observation("contact-b", GridCoord(7, 9), "UAV-2", observed_at=3.0)
    sm.record_target_observation("contact-a", GridCoord(6, 8), "UAV-1", observed_at=4.0)

    observation = build_observation(
        engine,
        control_owner=ControlOwner.HEURISTIC,
        current_time=5.0,
    )

    assert [contact.contact_id for contact in observation.contacts] == ["contact-a", "contact-b"]
    assert observation.contacts[0].estimated_position == (6.0, 8.0)
    assert observation.contacts[0].age_min == 1.0
    assert [hazard.hazard_id for hazard in observation.hazards] == sorted(
        hazard.hazard_id for hazard in observation.hazards
    )
    assert [base.base_id for base in observation.bases] == sorted(
        base.base_id for base in observation.bases
    )
    assert [uav.uav_id for uav in observation.shared_uavs] == sorted(
        uav.uav_id for uav in observation.shared_uavs
    )
    assert observation.action_mask.target_contact_ids == ("contact-a", "contact-b")
    assert observation.action_mask.allowed_sensor_modes == (
        SensorMode.OFF,
        SensorMode.SAR,
        SensorMode.EO,
    )
    assert observation.action_mask.allowed_operation_modes == (
        OperationMode.TRANSIT,
        OperationMode.COVERAGE,
        OperationMode.TRACK,
    )


def test_observation_action_mask_respects_return_lease(engine):
    observation = build_observation(
        engine,
        control_owner=ControlOwner.SYSTEM,
        operation_mode=OperationMode.RETURN,
    )

    assert observation.action_mask.allowed_sensor_modes == (SensorMode.OFF,)
    assert observation.action_mask.allowed_operation_modes == (
        OperationMode.RETURN,
        OperationMode.HOLDING,
    )


def test_observation_action_mask_respects_holding_recovery_authority(engine):
    observation = build_observation(
        engine,
        control_owner=ControlOwner.SYSTEM,
        operation_mode=OperationMode.HOLDING,
    )

    assert observation.action_mask.allowed_sensor_modes == (SensorMode.OFF,)
    assert observation.action_mask.allowed_operation_modes == (
        OperationMode.RETURN,
        OperationMode.HOLDING,
    )


def test_state_manager_versions_only_changed_obstacle_masks(engine):
    sm = engine.allocator.sm
    original_version = sm.obstacle_version
    same_mask = sm.obstacle_mask.copy()

    sm.set_environment_obstacles([], same_mask)
    assert sm.obstacle_version == original_version

    changed_mask = same_mask.copy()
    changed_mask[0, 0] = not changed_mask[0, 0]
    sm.set_environment_obstacles([], changed_mask)

    assert sm.obstacle_version == original_version + 1
    observation = build_observation(engine)
    assert observation.planning_map_version == sm.obstacle_version
    assert np.array_equal(observation.planning_obstacle_mask, changed_mask)


def test_observed_contact_can_be_applied_by_existing_control_registry(engine):
    from src.control.common.contracts import ControlCommand
    from src.control.common.operation_registry import OperationRegistry
    from src.env.ais_signal import AISSignal

    sm = engine.allocator.sm
    cid = sm.contacts.ingest_ais(AISSignal("123456789", (10., 10.), 0., 0., "MV", "Cargo", 0.), 0.)
    observation = build_observation(engine, control_owner=ControlOwner.HEURISTIC)
    registry = OperationRegistry(sm)
    registry.reconcile(engine.uavs[0].id, None,
                       ControlCommand(0., .2, SensorMode.EO, OperationMode.TRACK, cid),
                       observation)
    assert sm.get_track_regions()[0].target_group_id == cid


@pytest.mark.parametrize("legacy", [False, True])
def test_merged_alias_command_stays_valid_and_binds_the_canonical_contact(engine, legacy):
    from src.control.common.contracts import ControlCommand
    from src.control.common.operation_registry import OperationRegistry
    from tests.mission.test_contact_store import ais, visual

    sm = engine.allocator.sm
    if legacy:
        sm.record_target_observation("legacy-visual", GridCoord(10, 10), "UAV-1", 0)
        alias = "legacy-visual"
    else:
        alias = sm.contacts.ingest_visual(visual())
    aid = sm.contacts.ingest_ais(ais(), 0)
    registry = OperationRegistry(sm)
    command = ControlCommand(0., .2, SensorMode.EO, OperationMode.TRACK, alias)
    before = build_observation(engine, control_owner=ControlOwner.HEURISTIC)
    registry.reconcile(engine.uavs[0].id, None, command, before)
    sm.contacts.ingest_ais(ais(1), 1)
    sm.publish_contact_events()
    observation = build_observation(engine, control_owner=ControlOwner.HEURISTIC, current_time=1)

    assert {alias, aid} <= set(observation.action_mask.target_contact_ids)
    alias_observation = next(c for c in observation.contacts if c.contact_id == alias)
    assert alias_observation.group_id == aid
    registry.reconcile(engine.uavs[0].id, command, command, observation)
    assert len(sm.get_track_regions()) == 1
    assert sm.get_track_regions()[0].target_group_id == aid
    assert sm.get_uav(engine.uavs[0].id).target_group_id == aid
