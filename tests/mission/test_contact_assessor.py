"""Offline identity decisions must pass the real gateway and evidence gates."""
from dataclasses import replace
import importlib
import json
import math

import pytest

from src.env.ais_signal import AISSignal
from src.mission.contact_store import ContactStore
from src.mission.contracts import Assessment, ProbeSession, VisualDetection
from src.mission.llm_gateway import LLMGateway
from src.mission.trajectory_features import advance_probe, build_features
from src.schedule.config_loader import ConfigLoader


def api():
    assert importlib.util.find_spec("src.mission.contact_assessor") is not None, (
        "Task 8 contact assessor is missing")
    return importlib.import_module("src.mission.contact_assessor")


@pytest.fixture
def observed():
    config = ConfigLoader.load().mission.contact
    store = ContactStore(config, cell_size_km=config.cell_size_km)
    store.ingest_ais(AISSignal("123456789", (0., 0.), 0., 0., "MV Civil", "Cargo", 0.), 0.)
    for t in (*range(5), *range(6, 12)):
        distance = 1.8 if t < 5 else 1.2
        cid = store.ingest_visual(VisualDetection(
            f"EO:{t}", float(t), "eo", "U1", (.1 * t, 0.), (.1, 0.),
            .05, (.1 * t, distance), distance, "open_water"))
    store.reserve(cid, "U1", "P1")
    contact = store.snapshot(cid)
    probe = ProbeSession("P1", cid, "U1", "baseline", 0., None, 0., (), (), 0., None)
    probe = advance_probe(probe, contact.samples, 11., config)
    features = build_features(contact, probe, 11., config)
    assert probe.phase == "awaiting_assessment" and features.sufficient_evidence
    return store, contact, probe, features


def response(contact, probe, features, **changes):
    payload = dict(schema_version="contact-assessment/v1", contact_id=contact.contact_id,
                   probe_id=probe.probe_id, history_revision=contact.revision,
                   vessel_class="type_i", confidence=.85,
                   evidence_sample_ids=[features.baseline_sample_ids[0], features.near_sample_ids[-1]],
                   reasons=["Visual behavior remains steady before and during measured UAV approach."],
                   alternative_explanations=["A type II vessel could also maintain its course."])
    payload.update(changes)
    return payload


def test_assessment_payload_uses_canonical_vessel_class(observed):
    _, contact, probe, features = observed
    payload = response(contact, probe, features)

    assert "vessel_class" in payload
    assert "identity" not in payload
    assert api().validate_assessment_payload(payload, contact, probe, features) == ()


def make_assessor(scripted_transport, config, responses):
    transport = scripted_transport({"contact_assessor": responses})
    gateway = LLMGateway(transport=transport)
    return api().ContactAssessor(gateway=gateway, config=config), gateway, transport


@pytest.mark.parametrize("vessel_class", ["type_ii", "type_i", "unknown"])
def test_same_ais_can_yield_each_class_through_gateway(observed, scripted_transport, vessel_class):
    store, contact, probe, features = observed
    payload = response(contact, probe, features, vessel_class=vessel_class)
    assessor, gateway, transport = make_assessor(scripted_transport, store.config, [json.dumps(payload)])
    result = assessor.assess(contact, probe, features, 11.)
    assert result.vessel_class == vessel_class and result.confidence == .85
    assert result.assessment_id and result.assessed_at_min == 11.
    assert result.model_call_id == gateway.call_log[0]["call_id"]
    assert result.evidence_sample_ids == tuple(payload["evidence_sample_ids"])
    assert result.reasons == tuple(payload["reasons"])
    assert result.alternative_explanations == tuple(payload["alternative_explanations"])
    assert transport.calls[0]["role"] == "contact_assessor"
    assert transport.calls[0]["max_tokens"] == 2048
    store.apply_assessment(result)
    current = store.snapshot(contact.contact_id)
    assert current.vessel_class == vessel_class
    assert (current.state == "cleared") == (vessel_class == "type_i")
    if vessel_class == "unknown":
        assert current.assigned_uav_id == "U1" and current.cleared_at_min is None


@pytest.mark.parametrize("changes", [
    {"vessel_class": "military"}, {"vessel_class": []}, {"confidence": True},
    {"confidence": math.nan}, {"confidence": math.inf}, {"confidence": -.1},
    {"confidence": 1.1}, {"confidence": .79}, {"confidence": "0.9"},
    {"schema_version": "v0"}, {"contact_id": "foreign"}, {"probe_id": "foreign"},
    {"history_revision": 0}, {"history_revision": True}, {"history_revision": 12.0},
    {"evidence_sample_ids": ["foreign"]}, {"evidence_sample_ids": []},
    {"evidence_sample_ids": "EO:0"}, {"evidence_sample_ids": [{}]},
    {"reasons": ["a"] * 4}, {"reasons": ["x" * 201]}, {"reasons": [1]},
    {"reasons": "because"}, {"alternative_explanations": ["a"] * 4},
    {"alternative_explanations": ["x" * 201]},
    {"assessment_id": "model-invented"}, {"call_id": "model-invented"},
    {"assessed_at_min": 0.}, {"truth_identity": "type_i"},
])
def test_strict_response_schema_rejects_invalid_payload(observed, changes):
    _, contact, probe, features = observed
    assert api().validate_assessment_payload(response(contact, probe, features, **changes),
                                             contact, probe, features)


def test_unknown_low_confidence_and_text_boundaries_are_valid(observed):
    _, contact, probe, features = observed
    payload = response(contact, probe, features, vessel_class="unknown", confidence=0.,
                       reasons=["x" * 200] * 3, alternative_explanations=["y" * 200] * 3)
    assert api().validate_assessment_payload(payload, contact, probe, features) == ()
    for key in tuple(payload):
        incomplete = dict(payload)
        del incomplete[key]
        assert api().validate_assessment_payload(incomplete, contact, probe, features)


@pytest.mark.parametrize("case", ["ais_only", "near_incomplete", "insufficient", "stale_revision",
                                  "foreign_probe", "foreign_contact", "inactive_probe", "wrong_uav",
                                  "finished", "foreign_ids", "future", "timeout"])
def test_ineligible_evidence_never_calls_or_clears(observed, scripted_transport, case):
    store, contact, probe, features = observed
    now = 11.
    if case == "ais_only":
        contact = replace(contact, samples=tuple(s for s in contact.samples if s.source == "ais"))
        probe = replace(probe, phase="baseline", baseline_sample_ids=(), near_sample_ids=())
        features = build_features(contact, probe, now, store.config)
    elif case == "near_incomplete":
        probe = replace(probe, phase="near")
    elif case == "insufficient":
        features = replace(features, sufficient_evidence=False)
    elif case == "stale_revision":
        features = replace(features, history_revision=contact.revision - 1)
    elif case == "foreign_probe":
        features = replace(features, probe_id="P-other")
    elif case == "foreign_contact":
        probe = replace(probe, contact_id="C-other")
    elif case == "inactive_probe":
        contact = replace(contact, active_probe_id=None)
    elif case == "wrong_uav":
        contact = replace(contact, assigned_uav_id="U-other")
    elif case == "finished":
        probe = replace(probe, phase="finished")
    elif case == "foreign_ids":
        features = replace(features, near_sample_ids=("foreign",))
    elif case == "future":
        now = 8.
    elif case == "timeout":
        now = 100.
    assessor, _, transport = make_assessor(scripted_transport, store.config, [])
    assert assessor.assess(contact, probe, features, now) is None
    assert not transport.calls
    assert store.snapshot(contact.contact_id).identity == "unknown"
    assert store.snapshot(contact.contact_id).cleared_at_min is None


@pytest.mark.parametrize("changes", [
    {"source": "ais"}, {"source": "radar"}, {"source_id": "U-other"},
    {"contact_id": "C-other"}, {"observer_position_cells": None},
    {"measured_range_cells": None},
])
def test_forged_sufficient_flag_cannot_replace_visual_provenance(observed, scripted_transport, changes):
    store, contact, probe, features = observed
    contact = replace(contact, samples=tuple(replace(s, **changes) if s.source == "eo" else s
                                             for s in contact.samples))
    payload = response(contact, probe, features)
    assert api().validate_assessment_payload(payload, contact, probe, features)
    assessor, _, transport = make_assessor(scripted_transport, store.config, [])
    assert assessor.assess(contact, probe, features, 11.) is None
    assert not transport.calls


@pytest.mark.parametrize("phase", ["baseline", "near", "ais"])
def test_final_decisions_must_cite_both_visual_phases(observed, phase):
    _, contact, probe, features = observed
    ids = ([s.sample_id for s in contact.samples if s.source == "ais"] if phase == "ais"
           else list(getattr(features, f"{phase}_sample_ids")))
    for vessel_class in ("type_ii", "type_i"):
        assert api().validate_assessment_payload(
            response(contact, probe, features, vessel_class=vessel_class, evidence_sample_ids=ids),
            contact, probe, features)


@pytest.mark.parametrize("failure", ["invalid", "transport"])
def test_failure_keeps_unknown_and_revision_is_not_retried(observed, scripted_transport, failure):
    store, contact, probe, features = observed
    replies = ([json.dumps(response(contact, probe, features, evidence_sample_ids=["foreign"]))] * 3
               if failure == "invalid" else [TimeoutError("offline failure")] * 3)
    assessor, gateway, transport = make_assessor(scripted_transport, store.config, replies)
    assert assessor.assess(contact, probe, features, 11.) is None
    assert assessor.assess(contact, probe, features, 15.) is None
    assert len(gateway.call_log) == 1 and len(transport.calls) == 3
    assert store.snapshot(contact.contact_id).identity == "unknown"
    assert store.snapshot(contact.contact_id).cleared_at_min is None


def test_gateway_corrects_rejected_type_i_before_returning_unknown(observed, scripted_transport):
    store, contact, probe, features = observed
    invalid = response(contact, probe, features, evidence_sample_ids=["foreign"])
    valid = response(contact, probe, features, vessel_class="unknown", confidence=.3)
    assessor, gateway, _ = make_assessor(scripted_transport, store.config,
                                       [json.dumps(invalid), json.dumps(valid)])
    assert assessor.assess(contact, probe, features, 11.).vessel_class == "unknown"
    assert gateway.call_log[0]["validation_errors"]


def test_success_deduplicates_revision_and_respects_interval(observed, scripted_transport):
    store, contact, probe, features = observed
    newer = replace(contact, revision=contact.revision + 1)
    new_features = build_features(newer, probe, 14., store.config)
    assessor, gateway, _ = make_assessor(scripted_transport, store.config, [
        json.dumps(response(contact, probe, features)), json.dumps(response(newer, probe, new_features))])
    first = assessor.assess(contact, probe, features, 11.)
    assert assessor.assess(contact, probe, features, 14.) is None
    assert assessor.assess(newer, probe, new_features, 12.) is None
    second = assessor.assess(newer, probe, new_features, 14.)
    assert second.assessment_id != first.assessment_id
    assert second.model_call_id != first.model_call_id
    assert len(gateway.call_log) == 2
    restarted, _, transport = make_assessor(scripted_transport, store.config, [])
    assert restarted.assess(replace(contact, last_assessment=first), probe, features, 14.) is None
    assert not transport.calls


def test_prompt_has_explicit_observation_allowlists_and_no_internal_fields(observed, scripted_transport):
    store, contact, probe, features = observed
    # Extra attributes must not hitchhike through generic dataclass/object serialization.
    for obj in (contact, probe, features, *contact.samples):
        object.__setattr__(obj, "truth_identity", "DO-NOT-SEND")
    assessor, _, transport = make_assessor(scripted_transport, store.config,
                                         [json.dumps(response(contact, probe, features))])
    assessor.assess(contact, probe, features, 11.)
    messages = transport.calls[0]["messages"]
    payload = json.loads(messages[1]["content"])
    assert set(payload) == {"features", "keypoints", "uav_approach_history", "map_context"}
    assert set(payload["features"]) == {
        "contact_id", "history_revision", "probe_id", "baseline_sample_ids", "near_sample_ids",
        "baseline_duration_min", "near_duration_min", "baseline_speed_mean_kn", "near_speed_mean_kn",
        "baseline_abs_turn_rate_deg_min", "near_abs_turn_rate_deg_min", "heading_change_deg",
        "min_observed_uav_distance_cells", "close_exposure_min", "near_land_fraction",
        "max_observation_gap_min", "sufficient_evidence", "confounders"}
    assert len(payload["keypoints"]) <= 12
    assert all(set(s) == {"sample_id", "contact_id", "observed_at_min", "source", "source_id",
                          "position_cells", "velocity_cells_min", "position_uncertainty_cells",
                          "observer_position_cells", "measured_range_cells", "navigation_context"}
               for s in payload["keypoints"])
    assert payload["uav_approach_history"]
    assert all(set(s) == {"sample_id", "observed_at_min", "source_id", "observer_position_cells",
                          "measured_range_cells"} for s in payload["uav_approach_history"])
    text = json.dumps(messages)
    assert "DO-NOT-SEND" not in text and "_phase_samples" not in text
    prompt = messages[0]["content"].lower()
    for phrase in ("claimed", "silence", "position consistency", "before", "noise", "land",
                   "other uav", "200", "model confidence", "not a calibrated probability"):
        assert phrase in prompt


def test_prompt_uses_bounded_current_evidence_without_prior_conclusions(observed, scripted_transport):
    store, contact, probe, features = observed
    prior = Assessment("A-old", contact.contact_id, probe.probe_id, contact.revision - 1, 9.,
                       "type_ii", .99, (features.baseline_sample_ids[0],),
                       ("prior type_ii conclusion",), ("prior type_i alternative",), "call-old")
    contact = replace(contact, last_assessment=prior)
    config = replace(store.config, prompt_keypoints_per_contact=3)
    assessor, _, _ = make_assessor(scripted_transport, config, [])

    payload = assessor._prompt_payload(contact, probe, features)

    history = payload["uav_approach_history"]
    eligible_ids = set(features.baseline_sample_ids) | set(features.near_sample_ids)
    assert len(history) <= config.prompt_keypoints_per_contact
    reversed_payload = assessor._prompt_payload(
        replace(contact, samples=tuple(reversed(contact.samples))), probe, features)
    assert history == reversed_payload["uav_approach_history"]
    assert {sample["sample_id"] for sample in history} <= eligible_ids
    assert "prior type_ii conclusion" not in json.dumps(payload)
    assert "prior type_i alternative" not in json.dumps(payload)


def test_payload_rejects_excess_or_noneligible_visual_evidence(observed):
    _, contact, probe, features = observed
    outside = replace(next(s for s in contact.samples if s.source == "eo"),
                      sample_id="EO:outside", observed_at_min=20.)
    another = replace(outside, sample_id="EO:outside-2", observed_at_min=21.)
    contact = replace(contact, samples=(*contact.samples, outside, another))
    expanded = replace(features, baseline_sample_ids=(*features.baseline_sample_ids,
                                                       outside.sample_id, another.sample_id))
    eligible = list(expanded.baseline_sample_ids + expanded.near_sample_ids)

    excess = response(contact, probe, expanded, evidence_sample_ids=eligible)
    noneligible = response(contact, probe, features,
                           evidence_sample_ids=[features.baseline_sample_ids[0],
                                                features.near_sample_ids[-1], "EO:outside"])

    assert api().validate_assessment_payload(excess, contact, probe, expanded)
    assert api().validate_assessment_payload(noneligible, contact, probe, features)
