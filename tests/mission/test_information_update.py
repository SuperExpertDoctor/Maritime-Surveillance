
from src.mission.contracts import (
    EvidenceRecord,
    PointKernel,
    PassiveBearingObservation,
    PassivePosition,
)
from src.mission.information_update import InformationUpdatePolicy
from src.mission.information_update import ScanRefresh
from src.schedule.config_loader import ConfigLoader


def test_passive_position_updates_value_once_with_strength_one():
    policy = InformationUpdatePolicy(ConfigLoader.load())
    before = policy.snapshot(0.0).value[5][5]
    position = PassivePosition(
        position_id="POS-1",
        emitter_track_id="EMITTER-1",
        burst_id="BURST-1",
        sample_id="SAMPLE-1",
        observed_at_min=0.0,
        position_cells=(5.0, 5.0),
        source_observation_ids=("OBS-1", "OBS-2"),
    )

    delta = policy.apply_batch([position], now_min=0.0)
    after = policy.snapshot(0.0).value[5][5]

    assert delta is not None
    assert delta.version == 1
    assert delta.urgent is True
    assert after > before
    assert policy.apply_batch([position], now_min=0.0) is None
    assert policy.version == 1


def test_single_bearing_updates_ahead_corridor_without_point_position():
    policy = InformationUpdatePolicy(ConfigLoader.load())
    observation = PassiveBearingObservation(
        observation_id="OBS-1",
        sample_id="SAMPLE-1",
        emitter_track_id="EMITTER-1",
        burst_id="BURST-1",
        observed_at_min=0.0,
        observer_uav_id="UAV-1",
        observer_position_cells=(5.0, 5.0),
        bearing_deg=0.0,
        bearing_std_deg=3.0,
    )

    policy.apply_batch([observation], now_min=0.0)
    snapshot = policy.snapshot(0.0)

    assert snapshot.strategic[8][5] > snapshot.strategic[5][8]
    assert not policy.has_point_evidence("EMITTER-1")


def test_only_position_group_bearings_are_excluded_from_duplicate_gain():
    config = ConfigLoader.load()
    grouped = InformationUpdatePolicy(config)
    different_group = InformationUpdatePolicy(config)
    point_only = InformationUpdatePolicy(config)
    position = PassivePosition(
        position_id="POS-GROUP",
        emitter_track_id="EMITTER-1",
        burst_id="BURST-1",
        sample_id="SAMPLE-1",
        observed_at_min=0.0,
        position_cells=(8.0, 5.0),
        source_observation_ids=("OBS-GROUP-1", "OBS-GROUP-2"),
    )
    grouped_bearing = PassiveBearingObservation(
        observation_id="OBS-GROUP-1",
        sample_id="SAMPLE-1",
        emitter_track_id="EMITTER-1",
        burst_id="BURST-1",
        observed_at_min=0.0,
        observer_uav_id="UAV-1",
        observer_position_cells=(5.0, 5.0),
        bearing_deg=90.0,
        bearing_std_deg=3.0,
    )
    unrelated_bearing = PassiveBearingObservation(
        observation_id="OBS-OTHER",
        sample_id="SAMPLE-2",
        emitter_track_id="EMITTER-1",
        burst_id="BURST-2",
        observed_at_min=0.0,
        observer_uav_id="UAV-2",
        observer_position_cells=(5.0, 5.0),
        bearing_deg=0.0,
        bearing_std_deg=3.0,
    )

    grouped.apply_batch([position, grouped_bearing, unrelated_bearing], 0.0)
    different_group.apply_batch([position, unrelated_bearing], 0.0)
    point_only.apply_batch([position], 0.0)

    grouped_snapshot = grouped.snapshot(0.0)
    different_group_snapshot = different_group.snapshot(0.0)
    point_snapshot = point_only.snapshot(0.0)
    assert grouped_snapshot.strategic[5][8] == different_group_snapshot.strategic[5][8]
    assert different_group_snapshot.strategic[13][5] > point_snapshot.strategic[13][5]


def test_absolute_decay_is_independent_of_step_partition():
    config = ConfigLoader.load()
    record = EvidenceRecord(
        evidence_id="E-1",
        kind="passive_position",
        source_id="SRC-1",
        contact_id=None,
        observed_at_min=0.0,
        expires_at_min=15.0,
        strength=1.0,
        spatial=PointKernel((5.0, 5.0), 1.0),
    )
    whole = InformationUpdatePolicy(config)
    split = InformationUpdatePolicy(config)
    whole.apply_batch([record], 0.0)
    split.apply_batch([record], 0.0)
    whole.snapshot(8.0)
    for time in (1.0, 2.0, 4.0, 8.0):
        split.snapshot(time)
    assert whole.snapshot(8.0).value == split.snapshot(8.0).value


def test_invalid_batch_does_not_increment_version():
    policy = InformationUpdatePolicy(ConfigLoader.load())
    invalid = object()
    try:
        policy.apply_batch([invalid], now_min=0.0)
    except TypeError:
        pass
    assert policy.version == 0


def test_replayed_scan_refresh_does_not_increment_version():
    policy = InformationUpdatePolicy(ConfigLoader.load())
    refresh = ScanRefresh((1, 1, 4, 4), "search")

    first = policy.apply_batch([refresh], now_min=1.0)
    second = policy.apply_batch([refresh], now_min=1.0)

    assert first is not None
    assert second is None
    assert policy.version == 1


def test_decay_accumulates_until_material_delta_then_versions():
    policy = InformationUpdatePolicy(ConfigLoader.load())
    policy.apply_batch([ScanRefresh((1, 1, 2, 2), "search")], 0.0)
    version = policy.version

    assert policy.advance_time(0.1) is None
    delta = policy.advance_time(10.0)

    assert delta is not None
    assert delta.version == version + 1
    assert delta.max_abs_value_delta >= 0.05


def test_expiry_emits_dirty_bbox_and_reason():
    policy = InformationUpdatePolicy(ConfigLoader.load())
    evidence = EvidenceRecord(
        evidence_id="EXP-1",
        kind="passive_position",
        source_id="SRC-1",
        contact_id=None,
        observed_at_min=0.0,
        expires_at_min=2.0,
        strength=1.0,
        spatial=PointKernel((5.0, 5.0), 1.0),
    )
    policy.apply_batch([evidence], 0.0)

    delta = policy.advance_time(2.0)

    assert delta is not None
    assert "evidence_expired" in delta.reason_codes
    assert delta.cause_evidence_ids == ("EXP-1",)
    assert delta.changed_bbox is not None


def test_public_matrices_and_last_scan_time_are_policy_owned():
    policy = InformationUpdatePolicy(ConfigLoader.load())
    policy.apply_batch([ScanRefresh((2, 3, 4, 5), "track")], 7.0)

    assert policy.info_matrix(7.0).shape == (30, 30)
    assert policy.value_matrix(7.0).shape == (30, 30)
    assert policy.last_scan_time[2, 3] == 7.0
    assert policy.last_scan_time[3, 4] == 7.0
