import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from main import _parser
from src.control.bc.base import BCControllerBase
from src.control.common.contracts import (
    ActionSpec,
    ControlCommand,
    ControlDecision,
    ControlMode,
    OperationMode,
    ObservationSpec,
    SensorMode,
)
from src.env.simulation import SimulationEngine
from src.mission.episode_logger import EpisodeLogger
from src.mission.contracts import (
    CoverageConstraint,
    FeasibleEdge,
    Intent,
    IntentStatus,
    MissionSnapshot,
    TaskCandidate,
    UavResource,
)
from src.mission.outcome_evaluator import OutcomeEvaluator, EpisodeOutcome
from src.mission.strategy_memory import (
    StrategyMemory,
    StrategyMemoryStore,
    ValidationReport,
)
from src.schedule.config_loader import ConfigLoader
from src.mission.mission_scheduler import SELECTION_SCHEMA
from src.schedule.task_allocator import TaskAllocator
from src.schedule.trigger_manager import TriggerDecision


class _ReviewerDouble:
    def __init__(self, memory=None):
        self.memory = memory
        self.calls = []

    def step(self, current_time, state_manager):
        self.calls.append((current_time, state_manager))
        return self.memory


def _allocator_with_selection_provider():
    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    allocator.mission_scheduler.selection_provider = (
        lambda snapshot, payload: {
            "schema_version": SELECTION_SCHEMA,
            "snapshot_id": snapshot.snapshot_id,
            "selected_task_ids": [
                candidate["task_id"]
                for candidate in payload["snapshot"]["candidates"]
            ],
            "preempt_uav_ids": [],
            "defer_reason": None,
            "notes": "integration test",
        }
    )
    allocator.trigger_manager.check = lambda _time: TriggerDecision(
        "heavy", reason="integration test"
    )
    return allocator


def _coverage_capacity_satisfied_snapshot(*, candidates=None):
    resource = UavResource(
        "U1", (1.0, 1.0), 0.0, 1.0, 100.0, "idle", None, 0, 0.0,
    )
    ordinary = TaskCandidate(
        "search:spare", "search", (10, 10, 14, 14), None, (), ("U1",),
        0.0, "medium", 4.0, 0.5, 0.5,
    )
    tasks = (ordinary,) if candidates is None else tuple(candidates)
    return MissionSnapshot(
        "coverage-capacity-satisfied",
        10.0,
        tasks,
        ("U1",),
        (),
        (("U1", 0),),
        (resource,),
        tuple(
            FeasibleEdge(task.task_id, "U1", 1.0, 1.0, 1.0, 0.0, "test")
            for task in tasks
        ),
        (),
        (),
        (),
        (),
        "baseline",
        0,
        "",
        coverage_constraint=CoverageConstraint(
            desired_search_count=1,
            active_search_count=1,
            required_new_search_count=0,
            representative_task_ids=("search:spare",),
            must_service_task_ids=(),
        ),
    )


def test_mission_step_skips_satisfied_coverage_without_available_aircraft(
    monkeypatch,
):
    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    snapshot = replace(_coverage_capacity_satisfied_snapshot(), available_uav_ids=())
    allocator.reviewer = _ReviewerDouble()
    allocator.trigger_manager.check = lambda _time: TriggerDecision(
        "heavy", reason="coverage capacity test"
    )
    monkeypatch.setattr(
        allocator, "build_mission_snapshot", lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        allocator.mission_scheduler,
        "decide",
        lambda *_args, **_kwargs: pytest.fail("the model must not be called"),
    )

    result, batch = allocator.mission_step(10.0)

    assert batch is None
    assert result["trigger_type"] == "heavy"
    assert result["action"] == "mission_selection_skipped"
    assert result["skip_reason"] == "ordinary_search_capacity_satisfied"
    assert allocator.last_decision_timing["llm_seconds"] == 0.0
    assert allocator.sm.cycle == 1
    skipped = [
        event for event in allocator.sm.get_recent_events(0.0)
        if event["type"] == "mission_selection_skipped"
    ]
    assert skipped[-1]["data"] == {
        "reason": "ordinary_search_capacity_satisfied",
        "snapshot_id": snapshot.snapshot_id,
        "candidate_count": 1,
    }


def test_model_skip_only_applies_to_no_work_or_nonurgent_ordinary_searches():
    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    snapshot = _coverage_capacity_satisfied_snapshot()

    assert allocator._model_selection_skip_reason(snapshot) is None
    assert allocator._model_selection_skip_reason(
        replace(snapshot, available_uav_ids=())
    ) == "ordinary_search_capacity_satisfied"
    assert allocator._model_selection_skip_reason(
        replace(snapshot, candidates=(), feasible_edges=())
    ) == "no_model_candidates"

    probe = TaskCandidate(
        "probe:urgent", "probe", None, "contact-1", (), ("U1",),
        0.0, "high", 4.0, 1.0, 1.0,
    )
    assert allocator._model_selection_skip_reason(
        _coverage_capacity_satisfied_snapshot(candidates=(probe,))
    ) is None

    high_priority_search = replace(snapshot.candidates[0], priority="high")
    assert allocator._model_selection_skip_reason(
        _coverage_capacity_satisfied_snapshot(candidates=(high_priority_search,))
    ) is None


def test_mission_step_propagates_reviewer_summary_to_the_next_prompt():
    allocator = _allocator_with_selection_provider()
    reviewer = _ReviewerDouble("retain the observed priority order")
    allocator.reviewer = reviewer

    _result, batch = allocator.mission_step(1.0)

    assert batch is not None
    assert reviewer.calls
    payload = allocator.mission_scheduler.last_selection_payload
    assert payload["snapshot"]["reviewer_summary"] == reviewer.memory
    assert not allocator.uses_legacy_scheduler()


def test_reviewer_failure_keeps_unified_scheduler_and_existing_work_path():
    allocator = _allocator_with_selection_provider()
    allocator.reviewer = _ReviewerDouble(None)

    _result, batch = allocator.mission_step(1.0)

    assert batch is not None
    assert allocator.mission_scheduler.last_selection_success is True
    assert not allocator.uses_legacy_scheduler()


def test_light_snapshot_contains_intents_and_statuses():
    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    intent = Intent(
        "I-light", 1, "freshness", (8, 8, 12, 13), "search_priority", "high",
        1.0, 0.0, 30.0, None, "active",
    )
    status = IntentStatus(
        "I-light", 1, 1.0, 20, 0, 20, 0, 0.0, 0.0, None, (), "unserved",
    )

    allocator._handle_light_mission_trigger(
        1.0,
        TriggerDecision("light", reason="test"),
        (),
        intents=(intent,),
        intent_statuses=(status,),
    )

    assert allocator.last_mission_snapshot.intents == (intent,)
    assert allocator.last_mission_snapshot.intent_statuses == (status,)


def test_scheduler_mode_does_not_change_when_method_is_wrapped(monkeypatch):
    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    original_step = allocator.step
    monkeypatch.setattr(allocator, "step", lambda current_time: original_step(current_time))

    assert allocator.scheduler_mode == "mission"
    assert allocator.uses_legacy_scheduler() is False


def test_episode_logger_attaches_episode_id_to_every_record(tmp_path):
    logger = EpisodeLogger(tmp_path)
    episode_id = logger.start({"episode_id": "episode-integration-01"})
    logger.append("blue", "observations", {"sim_time_min": 1.0})
    logger.append("frames", "frames", {"frame_id": 1})
    logger.append("evaluation", "outcomes", {"valid": True})
    logger.finish("completed")

    for path in (
        tmp_path / episode_id / "blue" / "observations.jsonl",
        tmp_path / episode_id / "frames.jsonl",
        tmp_path / episode_id / "evaluation" / "outcomes.jsonl",
    ):
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["episode_id"] == episode_id


def test_evaluation_outcome_preserves_na_metrics_and_explicit_denominators():
    evaluator = OutcomeEvaluator("episode-metric-integration")
    evaluator.add_classification(truth="type_i", predicted="type_i")
    evaluator.add_classification(truth="type_ii", predicted="unknown")
    evaluator.register_handoff("handoff-1", at_min=4.0, successor_uav_ids=("U2",))
    evaluator.record_handoff_assignment("handoff-1", at_min=5.0)
    evaluator.record_handoff_lock("handoff-1", at_min=6.0)

    outcome = evaluator.finalize()

    assert outcome.type_i_recall == pytest.approx(1.0)
    assert outcome.type_ii_recall == pytest.approx(0.0)
    assert outcome.handoff_success_rate == pytest.approx(1.0)
    assert outcome.metric_denominators["type_i"] == 1
    assert outcome.metric_denominators["type_ii"] == 1
    assert outcome.metric_denominators["handoff_success_denominator"] == 1
    assert outcome.metric_denominators["continuous_observation_denominator_min"] == 0.0

    empty = OutcomeEvaluator("episode-metric-empty").finalize()
    assert not empty.valid
    assert empty.handoff_success_rate is None
    assert empty.continuous_observation_rate is None


def _active_memory_store(tmp_path):
    store = StrategyMemoryStore(tmp_path)
    memory = StrategyMemory(
        "M0001",
        1,
        "candidate",
        {},
        "Prioritize the oldest eligible probe before low-value coverage.",
        ("episode-1", "episode-2", "episode-3"),
        {"mean_score": 0.7},
        None,
        "2026-01-01T00:00:00+00:00",
    )
    store.save_candidate(memory)
    store.save_validation_report(ValidationReport(
        "R0001",
        "M0001",
        "baseline",
        (("validation-1", "candidate-1"),),
        (("holdout-1", "candidate-holdout-1"),),
        0.1,
        {"unique_coverage_ratio": 0.1},
        0.0,
        True,
        (),
    ))
    assert store.activate("M0001", "R0001") == "1"
    return store


def test_memory_version_active_is_resolved_once_and_survives_reset(tmp_path):
    store = _active_memory_store(tmp_path / "memory")
    config = ConfigLoader.load()

    engine = SimulationEngine(
        config,
        seed=41,
        llm_gateway=object(),
        strategy_memory_store=store,
        strategy_memory_version="active",
    )
    assert engine.allocator.memory_version == "1"
    assert engine.allocator.strategy_memory_store.select_for_context({}, "1")

    engine.reset(seed=42)
    assert engine.allocator.memory_version == "1"


def test_unknown_memory_version_fails_before_a_run_can_start(tmp_path):
    with pytest.raises(KeyError, match="unknown strategy version"):
        SimulationEngine(
            ConfigLoader.load(),
            seed=41,
            llm_gateway=object(),
            strategy_memory_store=StrategyMemoryStore(tmp_path),
            strategy_memory_version="missing-version",
        )


def test_runtime_entries_expose_the_same_memory_arguments():
    args = _parser().parse_args([
        "--no-server",
        "--skip-llm-probe",
        "--memory-version",
        "active",
        "--memory-root",
        "tmp-memory",
    ])

    assert args.memory_version == "active"
    assert args.memory_root == "tmp-memory"


class _RecordingBC(BCControllerBase):
    def __init__(self):
        self.calls = []

    @property
    def observation_spec(self):
        return ObservationSpec("control-observation/v2", 11)

    @property
    def action_spec(self):
        return ActionSpec(-1.0, 1.0, 0.1, 1.0)

    def load_policy(self, source):
        del source

    def encode_observation(self, observation):
        self.calls.append(("observation", observation))
        return observation

    def predict_action(self, encoded_observation):
        self.calls.append(("action", encoded_observation))
        return encoded_observation

    def decode_action(self, model_output):
        self.calls.append(("decoded", model_output))
        return ControlCommand(
            0.0,
            model_output.self_state.speed_cells_min,
            SensorMode.OFF,
            OperationMode.TRANSIT,
        )


def test_learning_provider_runs_factory_observation_action_safety_and_executor():
    config = ConfigLoader.load()
    config.control.per_uav["UAV-1"] = "bc"
    provider_instances = []

    def provider(_uav_id):
        controller = _RecordingBC()
        provider_instances.append(controller)
        return controller

    engine = SimulationEngine(
        config,
        seed=7,
        llm_gateway=object(),
        control_providers={ControlMode.BC: provider},
    )
    uav = engine.uavs[0]
    before = uav.float_position
    tick = engine.control_coordinator.step_uav(uav, current_time=1.0, dt_min=1.0)

    controller = provider_instances[0]
    assert [name for name, _ in controller.calls] == [
        "observation", "action", "decoded",
    ]
    assert tick.safety.applied_command.operation_mode is OperationMode.TRANSIT
    assert tick.execution is not None
    assert uav.float_position != before


def test_pending_search_without_selectable_candidates_does_not_call_model(monkeypatch):
    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    pending = SimpleNamespace(kind='search', status='approved', assigned_uav_id=None)
    snapshot = replace(_coverage_capacity_satisfied_snapshot(candidates=()),
                       active_tasks=(pending,), available_uav_ids=(), feasible_edges=())
    allocator.reviewer = _ReviewerDouble()
    allocator.trigger_manager.check = lambda _time: TriggerDecision('heavy', reason='pending search')
    monkeypatch.setattr(allocator, 'build_mission_snapshot', lambda *a, **kw: snapshot)
    monkeypatch.setattr(allocator.mission_scheduler, 'decide',
                        lambda *a, **kw: pytest.fail('pending search uses deterministic reassignment'))
    result, batch = allocator.mission_step(10.0)
    assert result['action'] == 'mission_selection_skipped'
    assert result['skip_reason'] == 'pending_search_reassignment'
    assert batch is None
    assert pending.status == 'approved'
    probe = SimpleNamespace(kind='probe', status='approved', assigned_uav_id=None)
    assert allocator._model_selection_skip_reason(replace(snapshot, active_tasks=(probe,))) is None
