from dataclasses import fields

import pytest

from src.mission.contact_assessor import ContactAssessor
from src.mission.contracts import (
    BearingKernel,
    ContactSnapshot,
    EvidenceRecord,
    InformationSnapshot,
    PassiveBearingObservation,
    PassivePosition,
    VisualDetection,
)
from src.mission.contact_store import ContactStore
from src.vis.backend.frame_builder import _evidence_snapshot
from src.schedule.config_loader import (
    ActivityConfig,
    EvasionConfig,
    PopulationConfig,
    allocate_population,
    ConfigLoader,
)


def test_largest_remainder_population_is_deterministic():
    assert allocate_population(3, {"civilian": 0.5, "research": 0.5}) == {
        "civilian": 2,
        "research": 1,
    }
    assert allocate_population(20, {"civilian": 0.7, "research": 0.3}) == {
        "civilian": 14,
        "research": 6,
    }


def test_alignment_config_sections_are_loaded_with_hardened_defaults():
    config = ConfigLoader.load()

    assert config.uav.count == config.uav.count_max
    assert config.ship.population.total_count == config.ship.initial_ship_count
    assert config.sensor.passive.detection_range_cells == 10.0
    assert config.sensor.emitter.burst_duration_min == (0.5, 2.0)
    assert isinstance(config.mission.activity, ActivityConfig)
    assert isinstance(config.mission.evasion, EvasionConfig)
    assert config.mission.information_update.value_alpha == pytest.approx(0.45)


def test_alignment_config_rejects_boolean_numeric_values():
    with pytest.raises(ValueError):
        PopulationConfig(total_count=True, civilian_ratio=0.5, research_ratio=0.5)


def test_passive_contract_does_not_have_range_or_power_fields():
    names = {field.name for field in fields(PassiveBearingObservation)}
    assert names == {
        "observation_id",
        "sample_id",
        "emitter_track_id",
        "burst_id",
        "observed_at_min",
        "observer_uav_id",
        "observer_position_cells",
        "bearing_deg",
        "bearing_std_deg",
    }


def test_passive_position_is_immutable_and_keeps_source_observations():
    observation = PassiveBearingObservation(
        observation_id="OBS-1",
        sample_id="episode-1:1",
        emitter_track_id="EMITTER-1",
        burst_id="BURST-1",
        observed_at_min=1.0,
        observer_uav_id="UAV-1",
        observer_position_cells=(1.0, 2.0),
        bearing_deg=90.0,
        bearing_std_deg=3.0,
    )
    position = PassivePosition(
        position_id="POS-1",
        emitter_track_id="EMITTER-1",
        burst_id="BURST-1",
        sample_id="episode-1:1",
        observed_at_min=1.0,
        position_cells=(5.25, 5.75),
        source_observation_ids=(observation.observation_id, "OBS-2"),
    )
    assert position.position_cells == (5.25, 5.75)
    assert position.source_observation_ids == ("OBS-1", "OBS-2")


def test_passive_bearing_frame_has_no_range_or_power_measurement():
    record = EvidenceRecord(
        evidence_id="BEARING-1",
        kind="passive_bearing",
        source_id="EMITTER-1",
        contact_id=None,
        observed_at_min=1.0,
        expires_at_min=11.0,
        strength=0.6,
        spatial=BearingKernel((4.0, 4.0), 90.0, 3.0, 0.5, 10.0),
    )

    payload = _evidence_snapshot(record)

    assert "range_decay_cells" not in payload
    assert "received_power_db" not in payload
    assert "position" not in payload


def test_information_snapshot_stores_immutable_field_matrices():
    snapshot = InformationSnapshot(
        version=1,
        frozen_at_min=0.0,
        info=((0.0,),),
        strategic=((0.0,),),
        timeliness=((0.0,),),
        value=((0.45,),),
        recent_deltas=(),
    )
    assert snapshot.value == ((0.45,),)


def test_research_class_does_not_imply_violation():
    result = ContactAssessor.assess_dimensions(({
        "evidence_id": "rad-1",
        "family": "eo_class",
        "vessel_class": "research",
        "passes_quality_gate": True,
        "strength": 1.0,
    },))

    assert result.vessel_class == "research"
    assert result.activity != "confirmed_violation"


def test_radiation_activity_does_not_establish_vessel_class():
    result = ContactAssessor.assess_dimensions(({
        "evidence_id": "rad-activity-1",
        "family": "radiation_activity",
        "passes_quality_gate": True,
        "strength": 1.0,
    },))

    assert result.vessel_class == "unknown"
    assert result.class_evidence_ids == ()
    assert result.activity_evidence_ids == ("rad-activity-1",)


def test_contact_snapshot_retains_dual_dimension_estimates_across_replace():
    snapshot = ContactSnapshot(
        "C-DIM", 1, "pending", "unknown", None, 0.0, 0.0, (4.0, 5.0),
        None, 0.2, None, None, None, None, 0.0, (),
        vessel_class="research",
        class_confidence=0.9,
        class_evidence_ids=("EO-CLASS-1",),
        activity="suspected_violation",
        activity_confidence=0.8,
        activity_evidence_ids=("MOTION-1",),
    )

    from dataclasses import replace

    updated = replace(snapshot, state="tracking")
    assert updated.vessel_class == "research"
    assert updated.class_evidence_ids == ("EO-CLASS-1",)
    assert updated.activity == "suspected_violation"


def test_passive_position_association_is_unique_and_tracks_distinct_bursts():
    config = ConfigLoader.load()
    store = ContactStore(config.mission.contact, cell_size_km=config.grid.cell_size_km)
    visual_id = store.ingest_visual(
        VisualDetection(
            "EO-DIM-1", 1.0, "eo", "UAV-1", (10.0, 10.0), (0.0, 0.0),
            0.4, (10.0, 8.0), 2.0, "open_water",
        )
    )
    first = PassivePosition(
        "POS-DIM-1", "EM-DIM", "BURST-1", "SAMPLE-1", 1.0,
        (10.2, 10.1), ("OBS-DIM-1", "OBS-DIM-2"),
    )
    second = PassivePosition(
        "POS-DIM-2", "EM-DIM", "BURST-2", "SAMPLE-2", 2.0,
        (10.3, 10.2), ("OBS-DIM-3", "OBS-DIM-4"),
    )
    assert store.ingest_passive_position(first, association_radius_cells=1.0) == visual_id
    assert store.ingest_passive_position(second, association_radius_cells=1.0) == visual_id
    assert store.passive_burst_count(visual_id, 2.0) == 2


def test_visual_observation_can_associate_after_passive_position():
    config = ConfigLoader.load()
    store = ContactStore(config.mission.contact, cell_size_km=config.grid.cell_size_km)
    position = PassivePosition(
        "POS-FIRST", "EM-FIRST", "BURST-FIRST", "SAMPLE-FIRST", 1.0,
        (10.0, 10.0), ("OBS-FIRST-1", "OBS-FIRST-2"),
    )

    signal_id = store.ingest_passive_position(position)
    visual_id = store.ingest_visual(
        VisualDetection(
            "EO-FIRST", 1.0, "eo", "UAV-1", (10.2, 10.1), (0.0, 0.0),
            0.4, (10.0, 8.0), 2.0, "open_water",
        )
    )

    assert visual_id == signal_id
    assert store.snapshot(signal_id).samples[-1].sample_id == "EO-FIRST"


def test_two_distinct_passive_bursts_emit_one_radiation_activity_fact():
    config = ConfigLoader.load()
    store = ContactStore(config.mission.contact, cell_size_km=config.grid.cell_size_km)
    first = PassivePosition(
        "POS-RAD-1", "EM-RAD", "BURST-1", "SAMPLE-RAD-1", 1.0,
        (6.0, 6.0), ("OBS-RAD-1", "OBS-RAD-2"),
    )
    second = PassivePosition(
        "POS-RAD-2", "EM-RAD", "BURST-2", "SAMPLE-RAD-2", 2.0,
        (6.1, 6.0), ("OBS-RAD-3", "OBS-RAD-4"),
    )

    store.ingest_passive_position(first)
    assert store.drain_radiation_activity_evidence() == ()
    contact_id = store.ingest_passive_position(second)
    evidence = store.drain_radiation_activity_evidence()

    assert len(evidence) == 1
    assert evidence[0].kind == "research_assessment"
    assert evidence[0].source_id == "EM-RAD"
    assert evidence[0].contact_id == contact_id
    assert store.drain_radiation_activity_evidence() == ()
