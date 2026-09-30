import json
from copy import deepcopy
from pathlib import Path

import yaml

from src.mission import mission_scheduler as mission_scheduler_module
from src.mission.contracts import (
    ContactSnapshot,
    FeasibleEdge,
    MissionSnapshot,
    ObservationSample,
    TaskCandidate,
    UavResource,
)
from src.mission.llm_gateway import LLMGateway, ModelResult
from src.mission.mission_scheduler import MissionScheduler, SELECTION_SCHEMA
from src.mission.prompt_payload import decode_selection_payload
from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from tests.mission.conftest import ScriptedTransport


def _task(task_id: str) -> TaskCandidate:
    return TaskCandidate(
        task_id,
        "search",
        (int(task_id[1:]) * 2, 0, int(task_id[1:]) * 2 + 1, 1),
        None,
        (),
        ("U1",),
        0.0,
        "medium",
        1.0,
        1.0,
        1.0,
    )


def _snapshot(*, candidate_count=60, contacts=(), resource_count=1):
    resources = tuple(
        UavResource(
            f"U{index}", (0.0, 0.0), 0.0, 1.0, 100.0,
            "idle", None, 0, 0.0,
        )
        for index in range(1, resource_count + 1)
    )
    tasks = tuple(_task(f"Q{index}") for index in range(candidate_count))
    edges = tuple(
        FeasibleEdge(task.task_id, "U1", 1.0, 1.0, 1.0, 0.0, task.task_id)
        for task in tasks
    )
    return MissionSnapshot(
        "budget-snapshot",
        10.0,
        tasks,
        tuple(resource.uav_id for resource in resources),
        (),
        tuple((resource.uav_id, resource.generation) for resource in resources),
        resources,
        edges,
        (),
        tuple(contacts),
        (),
        (),
        "baseline",
        0,
        reviewer_summary="reviewer " * 1000,
    )


def _selection(snapshot, task_id="Q0"):
    return {
        "schema_version": SELECTION_SCHEMA,
        "snapshot_id": snapshot.snapshot_id,
        "selected_task_ids": [task_id],
        "preempt_uav_ids": [],
        "defer_reason": None,
        "notes": "",
    }


def _contact(index: int) -> ContactSnapshot:
    samples = tuple(
        ObservationSample(
            f"sample-{index}-{sample_index}",
            f"C{index}",
            float(sample_index),
            "sar",
            f"U{(index % 3) + 1}",
            (float(sample_index), 0.0),
            (1.0, 0.0),
            0.1,
            (0.0, 0.0),
            1.0,
            "open_water",
        )
        for sample_index in range(600)
    )
    return ContactSnapshot(
        f"C{index}", 1, "pending", "unknown", None, 0.0, 599.0,
        (0.0, 0.0), (1.0, 0.0), 1.0, None, None, None, None, 0.0,
        samples,
    )


def test_prompt_serialization_is_bounded_without_mutating_snapshot():
    snapshot = _snapshot(contacts=tuple(_contact(index) for index in range(1000)))
    scheduler = MissionScheduler(
        max_tasks_in_prompt=40,
        selection_provider=lambda current, _payload: _selection(current),
    )

    assert scheduler.decide(snapshot) is not None

    prompt = scheduler.last_selection_payload["snapshot"]
    assert len(prompt["candidates"]) <= 40
    assert len(prompt["contacts"]) == 20
    assert all(len(contact["samples"]) <= 12 for contact in prompt["contacts"])
    assert len(snapshot.contacts) == 1000
    assert all(len(contact.samples) == 600 for contact in snapshot.contacts)
    assert scheduler.last_selection_timing["prompt_bytes"] < 100 * 1024
    assert set(scheduler.last_selection_timing) >= {
        "prompt_seconds", "prompt_bytes", "llm_seconds",
        "validation_seconds", "matching_seconds", "total_seconds",
    }
    json.dumps(scheduler.last_selection_payload, ensure_ascii=False, allow_nan=False)


def _write_llm_config(tmp_path, mutate):
    data = yaml.safe_load(Path("configs/llm_params.yaml").read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "llm_params.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


def test_scheduler_uses_configured_decision_maker_output_budget(tmp_path, monkeypatch):
    monkeypatch.setattr("src.mission.mission_scheduler.time.perf_counter", lambda: 100.0)
    monkeypatch.setattr("src.mission.llm_gateway.time.perf_counter", lambda: 100.0)
    snapshot = _snapshot(candidate_count=1)
    path = _write_llm_config(
        tmp_path,
        lambda data: data["bindings"]["decision_maker"].update(max_tokens=8192),
    )
    transport = ScriptedTransport({"decision_maker": [json.dumps(_selection(snapshot))]})
    scheduler = MissionScheduler(
        gateway=LLMGateway(path, transport=transport),
        planning_deadline_seconds=60.0,
        postprocess_reserve_seconds=1.0,
    )

    assert scheduler.decide(snapshot) is not None

    call = transport.calls[0]
    assert call["max_tokens"] == 8192
    assert call["thinking"] == "enabled"
    assert call["timeout_seconds"] == 59.0


def test_scheduler_default_decision_maker_output_budget_reaches_transport():
    snapshot = _snapshot(candidate_count=1)
    transport = ScriptedTransport({"decision_maker": [json.dumps(_selection(snapshot))]})
    scheduler = MissionScheduler(gateway=LLMGateway(transport=transport))

    assert scheduler.decide(snapshot) is not None
    assert transport.calls[0]["max_tokens"] == 4096


def test_prompt_budget_failure_names_the_largest_field():
    snapshot = _snapshot(candidate_count=1, resource_count=1200)
    scheduler = MissionScheduler(
        selection_provider=lambda current, _payload: _selection(current),
    )

    assert scheduler.decide(snapshot) is None
    assert scheduler.last_selection_errors[0].startswith("prompt_budget_exceeded:")
    assert scheduler.last_selection_failure_stage == "prompt"


class _RecordingOfflineGateway:
    """Capture decision requests while keeping the simulation fully offline."""

    def __init__(self):
        self.requests = []

    def request_json(self, **kwargs):
        if kwargs["role"] == "decision_maker":
            self.requests.append({
                "snapshot_id": kwargs["snapshot_id"],
                "system_prompt": kwargs["system_prompt"],
                "wire_payload": deepcopy(kwargs["user_payload"]),
            })
        return ModelResult(
            "offline", False, None, ("decision_deadline_exceeded",), "timeout",
        )


def test_seed_42_initial_scenario_wire_payload_is_lossless_and_under_30_kib(monkeypatch):
    gateway = _RecordingOfflineGateway()
    engine = SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=gateway)
    canonical_payloads = {}
    original_encode = mission_scheduler_module.encode_selection_payload

    def record_encode(payload):
        before = deepcopy(payload)
        wire = original_encode(payload)
        assert payload == before
        canonical_payloads.setdefault(before["snapshot"]["snapshot_id"], before)
        return wire

    monkeypatch.setattr(mission_scheduler_module, "encode_selection_payload", record_encode)

    # A committed fleet no longer retries the model every step: the
    # deterministic fallback covers failed calls between heavy triggers, so
    # requests arrive at the trigger cadence rather than once per step.
    for _ in range(120):
        if len(gateway.requests) >= 3:
            break
        engine.step()
        while engine.runtime_status == "paused_model":
            engine.retry_blocked_decision()

    assert len(gateway.requests) == 3
    for request in gateway.requests:
        canonical = canonical_payloads[request["snapshot_id"]]
        wire = request["wire_payload"]
        decoded = decode_selection_payload(wire)

        assert "instructions" not in wire
        assert decoded["snapshot"] == canonical["snapshot"]
        assert [item["task_id"] for item in decoded["snapshot"]["candidates"]] == [
            item["task_id"] for item in canonical["snapshot"]["candidates"]
        ]
        system_bytes = len(request["system_prompt"].encode("utf-8"))
        user_bytes = len(json.dumps(
            wire, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8"))
        assert system_bytes + user_bytes < 30 * 1024
