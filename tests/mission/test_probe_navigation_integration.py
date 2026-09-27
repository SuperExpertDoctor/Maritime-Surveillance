from __future__ import annotations

import math
from dataclasses import replace

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


def test_probe_post_move_eo_evidence_completes_baseline_without_relaxed_gates():
    from src.mission.trajectory_features import advance_probe
    from tests.mission.test_trajectory_features import sample

    config = ConfigLoader.load().mission.contact
    uav = UAVEntity("UAV-1", GridCoord(10, 10), endurance_h=8.0,
                    cruise_speed_kmh=160.0, cell_size_km=10.0)
    contact = ContactObservation(
        "C-INTEGRATION", "G-INTEGRATION", (12.0, 10.0), (0.0, 0.0),
        "UAV-reporting", 0.0, 0.0, 1.0,
    )
    controller = ProbeController(
        observation_spec=ObservationSpec("control-observation/v2", 11),
        action_spec=ActionSpec(-2.0, 2.0, 0.5, 1.0), contact_config=config,
    )
    controller.start_task(ControlTask(
        "probe:C-INTEGRATION", OperationMode.PROBE,
        target_contact_id=contact.contact_id, probe_id="P-INTEGRATION",
    ), _observation(uav, contact))
    executor = UAVDynamicsExecutor()
    probe = _probe()
    samples = {}
    for tick in range(160):
        observation = replace(_observation(uav, contact), timestamp_min=tick * 0.5,
                              probe=probe)
        decision = controller.act(observation)
        executor.execute(uav, decision.command, observation.dt_min)
        now = (tick + 1) * 0.5
        evidence = ()
        distance = math.dist(uav.float_position, contact.estimated_position)
        if decision.command.sensor_mode is SensorMode.EO and distance <= 2.5:
            packet = sample(now, distance=distance, source_id="UAV-1",
                            contact_id=contact.contact_id,
                            position_cells=contact.estimated_position,
                            observer_position_cells=uav.float_position)
            samples[packet.sample_id] = packet
            evidence = (packet,)
        probe = advance_probe(probe, evidence, now, config)
        if probe.phase in {"awaiting_assessment", "finished"}:
            break
    assert probe.phase == "awaiting_assessment"
    assert probe.close_exposure_min >= config.near_duration_min
    assert not set(probe.baseline_sample_ids) & set(probe.near_sample_ids)
    retained = [samples[key] for key in probe.baseline_sample_ids]
    assert len(retained) >= config.min_valid_samples_per_phase
    assert retained[-1].observed_at_min - retained[0].observed_at_min >= config.baseline_duration_min
    assert all(abs(item.measured_range_cells - config.baseline_standoff_cells) <= 0.15
               for item in retained)


def test_engine_probe_acquires_real_post_move_eo_baseline():
    from scripts.replay_restoration_scenarios import build_scenario

    engine = build_scenario("V07", seed=42, transport="fixture")
    engine._refresh_ais_signals(0.0)
    ship = next(item for item in engine.ships if engine._vessel_contact_ids.get(item.id))
    contact_id = next(iter(engine._vessel_contact_ids[ship.id]))
    uav = engine.uavs[0]
    probe = replace(_probe(), contact_id=contact_id, uav_id=uav.id)
    engine.allocator.sm.contacts.reserve(contact_id, uav.id, probe.probe_id)
    engine.allocator.sm.set_probe_session(probe)
    uav.target_group_id = contact_id
    uav._col, uav._row = ship.float_position[0] - 2.0, ship.float_position[1]
    uav.heading_rad = 0.0
    speed = uav.cruise_speed_kmh / uav.cell_size_km / 60.0
    assert math.isclose(speed, 0.26666666666666666)
    assert engine.clock.dt_min == 1.0
    controller = ProbeController(
        observation_spec=ObservationSpec("control-observation/v2", 11),
        action_spec=engine._control_action_spec(), contact_config=engine.config.mission.contact,
    )
    contact = ContactObservation(contact_id, contact_id, ship.float_position,
                                 (0.0, 0.0), "ais", 0.0, 0.0, 1.0)
    initial = _observation(uav, contact)
    initial = replace(initial, dt_min=1.0, probe=probe,
                      self_state=replace(initial.self_state, uav_id=uav.id, speed_cells_min=speed))
    controller.start_task(ControlTask("engine-probe", OperationMode.PROBE,
                                     target_contact_id=contact_id, probe_id=probe.probe_id), initial)
    executor = UAVDynamicsExecutor()
    observed_phases = set()
    for tick in range(1, 45):
        now = float(tick)
        engine.allocator.sm.current_time = now
        engine._refresh_ais_signals(now)
        current = engine.allocator.sm.get_probe_session(probe.probe_id)
        observation = replace(initial, timestamp_min=now - 1.0, probe=current,
                              self_state=replace(initial.self_state,
                                                 position=uav.float_position,
                                                 heading_rad=uav.heading_rad))
        decision = controller.act(observation)
        executor.execute(uav, decision.command, 1.0)
        post_move = uav.float_position
        engine._update_sensors_and_detections(now)
        history = engine.allocator.sm.contacts.snapshot(probe.contact_id)
        for packet in history.samples:
            if packet.source == "eo" and packet.source_id == uav.id and packet.observed_at_min == now:
                assert packet.observer_position_cells == post_move
                assert math.isclose(packet.measured_range_cells,
                                    math.dist(post_move, ship.float_position))
        engine._advance_probe_sessions(now)
        current = engine.allocator.sm.get_probe_session(probe.probe_id)
        if current is None:
            break
        observed_phases.add(current.phase)
        if current.phase in {"closing", "near", "awaiting_assessment", "finished"}:
            break
    assert observed_phases & {"closing", "near", "awaiting_assessment"}
    retained = [packet for packet in history.samples
                if packet.sample_id in current.baseline_sample_ids]
    assert len(retained) >= engine.config.mission.contact.min_valid_samples_per_phase
    assert all(packet.source == "eo" and packet.source_id == uav.id for packet in retained)
    assert retained[-1].observed_at_min - retained[0].observed_at_min >= engine.config.mission.contact.baseline_duration_min
