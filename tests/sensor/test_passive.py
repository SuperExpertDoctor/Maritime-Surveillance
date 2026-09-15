from src.mission.config import PassiveConfig
from src.sensor.passive import PassivePositionResolver, PassiveSensor


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
    assert not hasattr(observation, "received_power_db")
    assert not hasattr(observation, "range_cells")
    assert not hasattr(observation, "emitter_position_cells")
    assert PassivePositionResolver().release([observation], (5.0, 5.0)) is None


def test_hard_range_rejects_observation_without_consuming_detection_result():
    sensor = _sensor()
    assert _observe(sensor, "UAV-1", source=(11.0, 0.0)) is None


def test_two_distinct_uavs_same_group_release_sample_true_position():
    sensor = _sensor()
    observations = [_observe(sensor, "UAV-1"), _observe(sensor, "UAV-2")]

    position = PassivePositionResolver().release(observations, (5.25, 5.75))

    assert position is not None
    assert position.position_cells == (5.25, 5.75)
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
