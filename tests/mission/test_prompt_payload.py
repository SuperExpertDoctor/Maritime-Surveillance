from copy import deepcopy

import pytest

from src.mission.prompt_payload import (
    decode_selection_payload,
    encode_selection_payload,
)


def canonical_payload():
    return {
        "schema_version": "mission-selection/v1",
        "instructions": "rules",
        "snapshot": {
            "snapshot_id": "S1",
            "candidates": [{"task_id": "Q1", "feasible_uav_ids": ["U1"]}],
            "feasible_edges": [{
                "task_id": "Q1",
                "uav_options": [{
                    "uav_id": "U1",
                    "transit_time_min": 61.17751767083647,
                    "total_range_cells": 39.02193997489758,
                }],
            }],
            "contacts": [{"contact_id": "C1", "samples": []}],
            "coverage_constraint": {"must_service_task_ids": ["Q1"]},
        },
    }


def test_wire_payload_preserves_facts_without_mutating_source():
    canonical = canonical_payload()
    before = deepcopy(canonical)
    wire = encode_selection_payload(canonical)
    assert wire["prompt_format_version"] == "mission-prompt/v2"
    assert "instructions" not in wire
    assert "feasible_uav_ids" not in wire["snapshot"]["candidates"][0]
    assert wire["snapshot"]["feasible_edges"][0]["uav_options"] == [
        ["U1", 61.17751767083647, 39.02193997489758]
    ]
    decoded = decode_selection_payload(wire)
    assert decoded["snapshot"] == canonical["snapshot"]
    wire["snapshot"]["contacts"][0]["samples"].append("changed")
    assert canonical == before


def test_empty_options_round_trip_and_preserve_empty_eligibility():
    payload = canonical_payload()
    payload["snapshot"]["candidates"][0]["feasible_uav_ids"] = []
    payload["snapshot"]["feasible_edges"][0]["uav_options"] = []
    assert decode_selection_payload(encode_selection_payload(payload))["snapshot"] == payload["snapshot"]


def test_shared_uav_is_restored_per_task():
    payload = canonical_payload()
    second_candidate = {"task_id": "Q2", "feasible_uav_ids": ["U1"]}
    second_edge = deepcopy(payload["snapshot"]["feasible_edges"][0])
    second_edge["task_id"] = "Q2"
    payload["snapshot"]["candidates"].append(second_candidate)
    payload["snapshot"]["feasible_edges"].append(second_edge)
    restored = decode_selection_payload(encode_selection_payload(payload))
    assert restored["snapshot"]["candidates"] == payload["snapshot"]["candidates"]


def test_mismatched_candidate_eligibility_is_retained():
    payload = canonical_payload()
    payload["snapshot"]["candidates"][0]["feasible_uav_ids"] = ["U2"]
    wire = encode_selection_payload(payload)
    assert wire["snapshot"]["candidates"][0]["feasible_uav_ids"] == ["U2"]
    assert decode_selection_payload(wire)["snapshot"]["candidates"][0]["feasible_uav_ids"] == ["U2"]


def test_malformed_option_row_width_is_rejected():
    wire = encode_selection_payload(canonical_payload())
    wire["snapshot"]["feasible_edges"][0]["uav_options"][0] = ["U1"]
    with pytest.raises(ValueError):
        decode_selection_payload(wire)


def test_unsupported_format_version_is_rejected():
    payload = canonical_payload()
    payload["prompt_format_version"] = "mission-prompt/v99"
    with pytest.raises(ValueError, match="unsupported mission prompt format"):
        decode_selection_payload(payload)


def test_legacy_payload_is_returned_untouched_and_detached():
    payload = canonical_payload()
    before = deepcopy(payload)
    decoded = decode_selection_payload(payload)
    assert decoded == payload == before
    decoded["snapshot"]["contacts"].clear()
    assert payload == before


def test_unsupported_option_fields_are_rejected():
    payload = canonical_payload()
    payload["snapshot"]["feasible_edges"][0]["uav_options"][0]["extra"] = 1
    with pytest.raises(ValueError, match="unsupported UAV option fields"):
        encode_selection_payload(payload)
