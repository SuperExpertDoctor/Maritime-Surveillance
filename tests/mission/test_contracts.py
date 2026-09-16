from dataclasses import FrozenInstanceError, asdict, fields

import pytest

import src.mission.contracts as contracts
from src.mission.contracts import (
    Assessment,
    ContactSnapshot,
    Intent,
    MissionSelection,
    ObservationSample,
    ProbeSession,
    RedMotionParameters,
    RedPlan,
    TaskCandidate,
    ship_rng_manifest,
)


@pytest.mark.parametrize(
    ("contract", "expected_fields"),
    [
        (
            ObservationSample,
            (
                "sample_id",
                "contact_id",
                "observed_at_min",
                "source",
                "source_id",
                "position_cells",
                "velocity_cells_min",
                "position_uncertainty_cells",
                "observer_position_cells",
                "measured_range_cells",
                "navigation_context",
            ),
        ),
        (
            Assessment,
            (
                "assessment_id",
                "contact_id",
                "probe_id",
                "history_revision",
                "assessed_at_min",
                    "vessel_class",
                "confidence",
                "evidence_sample_ids",
                "reasons",
                "alternative_explanations",
                "model_call_id",
            ),
        ),
        (
            ContactSnapshot,
            (
                "contact_id",
                "revision",
                "state",
                "identity",
                "ais_mmsi",
                "first_seen_min",
                "last_seen_min",
                "estimated_position_cells",
                "estimated_velocity_cells_min",
                "uncertainty_cells",
                "assigned_uav_id",
                "active_probe_id",
                "last_assessment",
                "cleared_at_min",
                "next_probe_not_before_min",
                "samples",
            ),
        ),
        (
            ProbeSession,
            (
                "probe_id",
                "contact_id",
                "uav_id",
                "phase",
                "started_at_min",
                "baseline_started_at_min",
                "phase_started_at_min",
                "baseline_sample_ids",
                "near_sample_ids",
                "close_exposure_min",
                "completed_reason",
            ),
        ),
        (
            Intent,
            (
                "intent_id",
                "revision",
                "label",
                "bbox",
                "mode",
                "priority",
                "weight",
                "created_at_min",
                "expires_at_min",
                "revisit_interval_min",
                "lifecycle",
            ),
        ),
        (
            TaskCandidate,
            (
                "task_id",
                "kind",
                "bbox",
                "contact_id",
                "intent_ids",
                "feasible_uav_ids",
                "eligible_since_min",
                "priority",
                "estimated_duration_min",
                "utility",
                "expected_information_gain",
            ),
        ),
        (
            MissionSelection,
            (
                "schema_version",
                "snapshot_id",
                "selected_task_ids",
                "preempt_uav_ids",
                "defer_reason",
                "notes",
            ),
        ),
        (
            RedMotionParameters,
            (
                "ship_id",
                "heading_offset_deg",
                "speed_kn",
                "zigzag_heading_deg",
                "zigzag_period_min",
                "phase_deg",
            ),
        ),
        (
            RedPlan,
            ("schema_version", "snapshot_id", "valid_for_min", "commands", "notes"),
        ),
    ],
)
def test_design_contract_fields_are_frozen(contract, expected_fields):
    assert tuple(field.name for field in fields(contract)
                 if not field.name.startswith("_")) == expected_fields
    assert contract.__dataclass_params__.frozen is True


def test_contact_snapshot_does_not_expose_environment_truth():
    sample = ObservationSample(
        sample_id="S0001",
        contact_id="C0001",
        observed_at_min=1.0,
        source="ais",
        source_id="123456789",
        position_cells=(4.0, 5.0),
        velocity_cells_min=None,
        position_uncertainty_cells=0.05,
        observer_position_cells=None,
        measured_range_cells=None,
        navigation_context="open_water",
    )
    snapshot = ContactSnapshot(
        contact_id="C0001",
        revision=1,
        state="pending",
        identity="unknown",
        ais_mmsi="123456789",
        first_seen_min=1.0,
        last_seen_min=1.0,
        estimated_position_cells=(4.0, 5.0),
        estimated_velocity_cells_min=None,
        uncertainty_cells=0.05,
        assigned_uav_id=None,
        active_probe_id=None,
        last_assessment=None,
        cleared_at_min=None,
        next_probe_not_before_min=1.0,
        samples=(sample,),
    )

    serialized = asdict(snapshot)
    prohibited = {
        "ship_id",
        "physical_ship_id",
        "actual_military",
        "truth_identity",
        "is_evading",
        "red_motion_parameters",
    }
    assert prohibited.isdisjoint(serialized)
    with pytest.raises(FrozenInstanceError):
        snapshot.identity = "target"


def test_ship_truth_is_owned_by_the_environment_not_public_mission_contracts():
    from src.env import ship as ship_module

    assert not hasattr(contracts, "ShipTruth")
    assert "ShipTruth" not in contracts.__all__

    truth = ship_module.ShipTruth(
        ship_id="V0001",
        vessel_class="type_ii",
        ais_enabled=False,
        normal_route=((1.0, 2.0, 0.5), (2.0, 3.0, 0.75)),
    )

    assert asdict(truth) == {
        "ship_id": "V0001",
        "vessel_class": "type_ii",
        "ais_enabled": False,
        "normal_route": ((1.0, 2.0, 0.5), (2.0, 3.0, 0.75)),
        "activity_schedule": (),
    }
    assert "Ship" not in vars(contracts)
    assert "SimulationEngine" not in vars(contracts)


def test_ship_identity_and_ais_rng_substreams_are_independent_and_recordable():
    manifest = ship_rng_manifest(42)

    assert manifest == ship_rng_manifest(42)
    assert set(manifest) == {"ship_class", "ship_ais_enabled"}
    assert manifest["ship_class"] != manifest["ship_ais_enabled"]
    assert all(isinstance(seed, int) for seed in manifest.values())
