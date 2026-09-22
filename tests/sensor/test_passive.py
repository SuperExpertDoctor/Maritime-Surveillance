import math
from dataclasses import replace

import pytest

from src.mission.config import PassiveConfig
from src.sensor.passive import PassivePositionResolver, PassiveSensor
from src.mission.contracts import PassiveBearingObservation


def _sensor():
    return PassiveSensor(
        PassiveConfig(
            reference_detection_probability=1.0,
            detection_range_cells=10.0,
            range_scale_cells=1000.0,
            bearing_std_deg=3.0,
            received_power_std_db=0.0,
            minimum_received_power_db=-90.0,
        ),
        seed=5,
    )


def _observe(sensor, observer, source=(5.0, 5.0), *, sample="S1", burst="B1"):
    return sensor.observe(
        sample,
        observer,
        observer_position_cells=(0.0, 0.0) if observer == "UAV-1" else (0.0, 1.0),
        emitter_track_id="E1",
        burst_id=burst,
        emitter_position_cells=source,
        source_power_at_reference_db=-40.0,
        observed_at_min=1.0,
    )


def test_single_uav_publishes_bearing_without_range_power_or_position():
    observation = _observe(_sensor(), "UAV-1")

    assert observation is not None
    assert observation.observer_uav_id == "UAV-1"
    assert observation.bearing_deg != 45.0
    assert not hasattr(observation, "received_power_db")
    assert not hasattr(observation, "range_cells")
    assert not hasattr(observation, "emitter_position_cells")
    assert PassivePositionResolver().release([observation], (5.0, 5.0)) is None


def test_hard_range_rejects_observation_without_consuming_detection_result():
    sensor = _sensor()
    assert _observe(sensor, "UAV-1", source=(11.0, 0.0)) is None


def test_two_distinct_uavs_same_group_release_sample_noisy_position_without_truth():
    sensor = _sensor()
    observations = [_observe(sensor, "UAV-1"), _observe(sensor, "UAV-2")]

    position = PassivePositionResolver().release(observations, (500.0, 500.0))

    assert position is not None
    assert math.dist(position.position_cells, (500.0, 500.0)) > 100.0
    assert math.isfinite(position.position_cells[0])
    assert math.isfinite(position.position_cells[1])
    assert position.source_observation_ids == tuple(
        item.observation_id for item in sorted(observations, key=lambda item: item.observer_uav_id)
    )


def test_duplicate_uav_or_different_group_never_releases_position():
    sensor = _sensor()
    one = _observe(sensor, "UAV-1")
    duplicate = _observe(sensor, "UAV-1")
    different_sample = _observe(sensor, "UAV-2", sample="S2")
    resolver = PassivePositionResolver()

    assert resolver.release([one, duplicate], (5.0, 5.0)) is None
    assert resolver.release([one, different_sample], (5.0, 5.0)) is None


def test_passive_position_uses_measured_bearings_instead_of_emitter_truth():
    sensor = _sensor()
    observations = [_observe(sensor, "UAV-1"), _observe(sensor, "UAV-2")]

    estimated = PassivePositionResolver().release(observations, (5.0, 5.0))

    assert estimated is not None
    assert estimated.position_cells != (5.0, 5.0)


def _bearing(observer, origin, angle):
    return PassiveBearingObservation(
        f"OBS-{observer}", "S1", "E1", "B1", 1.0, observer, origin, angle, 3.0,
    )


@pytest.mark.parametrize("change", [
    {"sample_id": "S2"}, {"emitter_track_id": "E2"}, {"burst_id": "B2"},
    {"observed_at_min": 2.0}, {"observation_id": "OBS-U1"},
])
def test_release_requires_same_sample_source_burst_time_and_unique_reports(change):
    one = _bearing("U1", (0.0, 0.0), 45.0)
    two = replace(_bearing("U2", (0.0, 2.0), 0.0), **change)
    assert PassivePositionResolver().release([one, two]) is None


@pytest.mark.parametrize("origins,angles", [
    (((0.0, 0.0), (0.0, 0.0)), (0.0, 90.0)),
    (((0.0, 0.0), (-0.5, -1.0)), (0.0, 90.0)),
    (((0.0, 0.0), (0.0, 2.0)), (0.0, 0.0)),
])
def test_degenerate_or_backward_bearing_geometry_never_releases(origins, angles):
    observations = [_bearing(f"U{i}", origin, angle)
                    for i, (origin, angle) in enumerate(zip(origins, angles))]
    assert PassivePositionResolver().release(observations) is None


def test_range_gate_does_not_advance_random_stream():
    sensor = _sensor()
    assert _observe(sensor, "UAV-1", source=(11.0, 0.0)) is None
    assert _observe(sensor, "UAV-1") == _observe(_sensor(), "UAV-1")


def test_triangulation_respects_configured_receiver_range():
    observations = [_bearing("U1", (0.0, 0.0), 0.0),
                    _bearing("U2", (0.0, 1.0), math.degrees(math.atan2(-1.0, 100.0)))]
    assert PassivePositionResolver(detection_range_cells=10.0).release(observations) is None


@pytest.mark.parametrize("active,burst,power,probability", [
    (False, "B1", -40.0, 1.0),
    (True, None, -40.0, 1.0),
    (True, "B1", -200.0, 1.0),
    (True, "B1", -40.0, 0.0),
])
def test_inactive_missing_burst_weak_or_missed_signal_has_no_observation(
    active, burst, power, probability,
):
    sensor = PassiveSensor(replace(_sensor().config,
                                   reference_detection_probability=probability), seed=5)
    assert sensor.observe("S1", "U1", (0.0, 0.0), "E1", burst,
                          (2.0, 2.0), power, 1.0, burst_active=active) is None


def test_exact_duplicate_report_is_idempotent_and_does_not_add_a_receiver():
    one = _bearing("U1", (0.0, 0.0), 45.0)
    two = _bearing("U2", (0.0, 2.0), 0.0)
    resolver = PassivePositionResolver()
    assert resolver.release([one, one]) is None
    assert resolver.release([one, two, one]) == resolver.release([one, two])
