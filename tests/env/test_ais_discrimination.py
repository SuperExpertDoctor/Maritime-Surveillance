import math

import pytest

from src.env.ais_signal import generate_ais_signal
from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import GridCoord
from src.utils.ais_discriminator import AISDiscriminator, EOMeasurement
from tests.mission.conftest import ScriptedTransport


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setenv("LONGCAT_API_KEY", "task-8-offline-fixture")
    transport = ScriptedTransport({})
    monkeypatch.setattr("src.mission.llm_gateway.OpenAICompatibleTransport.complete", transport.complete)
    return SimulationEngine(ConfigLoader.load(), seed=42)


@pytest.mark.parametrize("index", range(20))
def test_eo_position_estimation_is_only_a_measurement(index):
    pose = (2.0 + index * 0.1, 3.0 + index * 0.05, math.radians(15 + index))
    target = (4.0 + index * 0.1, 4.5 + index * 0.05)
    bearing = math.atan2(target[1] - pose[1], target[0] - pose[0]) - pose[2]
    measurement = EOMeasurement(bearing, math.dist(pose[:2], target))
    estimated = AISDiscriminator.estimate_target_position(pose, measurement)
    assert estimated == pytest.approx(target)
    assert AISDiscriminator.estimate_target_position(pose, {
        "relative_bearing_rad": bearing, "distance_cells": measurement.distance_cells,
    }) == pytest.approx(target)


def test_legacy_ais_identity_decision_paths_are_removed():
    # Task 8 replaces the old silent=>military and consistent=>civilian rules.
    assert not hasattr(AISDiscriminator, "discriminate")
    assert not hasattr(AISDiscriminator, "discriminate_formation")


@pytest.mark.parametrize("ais_enabled", [True, False])
def test_ais_state_remains_unknown_until_validated_observation(engine, ais_enabled):
    ship = next(item for item in engine.ships if item.vessel_class == "type_ii")
    # Establish a claimed contact before silence; silence itself is not evidence.
    ship.set_ais_enabled(True)
    group_id = engine.allocator.sm.contacts.ingest_ais(generate_ais_signal(ship, 0), 0)
    ship.set_ais_enabled(ais_enabled)
    assert engine.allocator.sm.contacts.snapshot(group_id).vessel_class == "unknown"
    center = ship.float_position  # sensor fixture geometry, never a blue lookup
    uav = engine.uavs[0]
    uav.position = GridCoord(int(round(center[0] - 1)), int(round(center[1])))
    uav._col, uav._row = center[0] - 1.8, center[1]
    uav.heading_rad = 0.0
    uav.status = "tracking"
    uav.target_group_id = group_id
    track = engine.allocator.sm.create_track_region(group_id, GridCoord(int(center[0]), int(center[1])))
    track.assigned_uav_id = uav.id
    engine.allocator.sm.update_uav_status(
        uav.id, "tracking", uav.position,
        assigned_region_id=track.id, target_group_id=group_id,
    )

    engine._update_sensors_and_detections(0.0)
    engine._update_sensors_and_detections(1.0)
    engine._update_sensors_and_detections(2.0)

    assert engine.allocator.sm.get_track_region_for_group(group_id) is not None
    assert engine.allocator.sm.get_target_report(group_id) is not None
    assert any(s.source == "eo" for s in engine.allocator.sm.contacts.snapshot(group_id).samples)
    assert engine.allocator.sm.contacts.snapshot(group_id).vessel_class == "unknown"
    assert ship.is_military is None
    assert ship.discrimination is None
    assert engine.ais_discriminations == 0
    assert engine.civilian_releases == 0
    assert not engine.allocator.sm.get_active_markers()


def test_departed_target_releases_track_without_marker(engine):
    group_id = engine.allocator.sm.contacts.list_snapshots()[0].contact_id
    uav = engine.uavs[0]
    center = engine._contact_center(group_id)
    uav.status = "tracking"
    uav.target_group_id = group_id
    track = engine.allocator.sm.create_track_region(group_id, GridCoord(int(center[0]), int(center[1])))
    track.assigned_uav_id = uav.id

    engine._release_departed_group(group_id, 3.0)

    assert engine.allocator.sm.get_track_region_for_group(group_id) is None
    assert not engine.allocator.sm.get_active_markers()
    assert uav.target_group_id is None
