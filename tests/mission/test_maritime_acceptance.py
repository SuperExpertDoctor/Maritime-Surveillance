from src.env.emitter import EmitterState
from src.env.simulation import SimulationEngine
from src.mission.contracts import (
    AssignmentBatch,
    PassiveBearingObservation,
    PassivePosition,
)
from src.mission.mission_scheduler import MissionScheduler
from src.mission.task_catalog import TaskCatalog
from src.schedule.candidate_extractor import CandidateResult
from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager
from src.schedule.task_allocator import TaskAllocator


class _AlwaysOnEmitter:
    def advance(self, _end_min):
        return ()

    def current_burst_at(self, _at_min):
        return EmitterState(True, 99.0, "BURST-1", 0.0)


class _EmptyExtractor:
    def extract(self, *_args, **_kwargs):
        return CandidateResult()


def test_simulation_does_not_publish_passive_truth_before_first_step(monkeypatch):
    monkeypatch.setenv("LONGCAT_API_KEY", "acceptance-offline")
    engine = SimulationEngine(ConfigLoader.load(), seed=101)

    assert engine.allocator.sm.get_passive_observations() == ()
    assert engine.allocator.sm.get_passive_positions() == ()
    assert engine.allocator.sm.information_version == 0


def test_two_uav_passive_release_updates_field_and_creates_investigation_task():
    config = ConfigLoader.load()
    state = StateManager(config)
    position = PassivePosition(
        position_id="POS-ACCEPT-1",
        emitter_track_id="EMITTER-ACCEPT-1",
        burst_id="BURST-1",
        sample_id="SAMPLE-1",
        observed_at_min=1.0,
        position_cells=(5.0, 5.0),
        source_observation_ids=("OBS-1", "OBS-2"),
    )

    state.current_time = 1.0
    delta = state.apply_information_facts([position], 1.0)
    state.register_passive_position(position)

    assert delta is not None
    assert state.information_version == 1
    assert state.get_passive_positions(1.0) == (position,)
    assert state.get_value_matrix()[5, 5] > 0.45

    catalog = TaskCatalog(candidate_extractor=_EmptyExtractor())
    tasks = catalog.build(state, (), (), now_min=1.0)
    investigation = next(
        task for task in tasks
        if task.task_id == "investigation:EMITTER-ACCEPT-1"
    )
    assert investigation.bbox is not None
    assert investigation.priority == "high"


def test_investigation_selection_and_assignment_use_one_information_version():
    config = ConfigLoader.load()
    allocator = TaskAllocator(config, llm_gateway=object())
    allocator.task_catalog = TaskCatalog(candidate_extractor=_EmptyExtractor())
    position = PassivePosition(
        position_id="POS-ACCEPT-2",
        emitter_track_id="EMITTER-ACCEPT-2",
        burst_id="BURST-2",
        sample_id="SAMPLE-2",
        observed_at_min=1.0,
        position_cells=(4.0, 4.0),
        source_observation_ids=("OBS-3", "OBS-4"),
    )
    allocator.sm.apply_information_facts([position], 1.0)
    allocator.sm.register_passive_position(position)
    snapshot = allocator.build_mission_snapshot(1.0)
    task = next(
        item for item in snapshot.candidates
        if item.task_id == "investigation:EMITTER-ACCEPT-2"
    )
    edge = next(edge for edge in snapshot.feasible_edges if edge.task_id == task.task_id)

    scheduler = MissionScheduler(
        gateway=object(),
        selection_provider=lambda _snapshot, _payload: {
            "schema_version": "mission-selection/v1",
            "snapshot_id": snapshot.snapshot_id,
            "selected_task_ids": [task.task_id],
            "preempt_uav_ids": [],
            "defer_reason": None,
            "notes": "fixture investigation",
            "information_version": snapshot.information_version,
        },
    )
    batch = scheduler.decide(snapshot)

    assert isinstance(batch, AssignmentBatch)
    assert batch.information_version == snapshot.information_version == 1
    assert batch.assignments[0].uav_id == edge.uav_id


def test_single_passive_bearing_creates_direction_search_without_position():
    config = ConfigLoader.load()
    state = StateManager(config)
    observation = PassiveBearingObservation(
        observation_id="OBS-DIRECTION-1",
        sample_id="SAMPLE-DIRECTION-1",
        emitter_track_id="EMITTER-DIRECTION-1",
        burst_id="BURST-DIRECTION-1",
        observed_at_min=1.0,
        observer_uav_id="UAV-1",
        observer_position_cells=(4.0, 4.0),
        bearing_deg=0.0,
        bearing_std_deg=3.0,
    )
    state.current_time = 1.0
    state.record_passive_observations((observation,))
    delta = state.apply_information_facts([observation], 1.0)

    catalog = TaskCatalog(candidate_extractor=_EmptyExtractor())
    tasks = catalog.build(state, (), (), now_min=1.0)
    direction = next(
        task for task in tasks
        if task.task_id == "direction:OBS-DIRECTION-1"
    )

    assert delta is not None
    assert state.get_passive_positions(1.0) == ()
    assert direction.kind == "direction_search"
    assert direction.priority == "high"


def test_position_only_suppresses_its_group_bearing_task():
    config = ConfigLoader.load()
    state = StateManager(config)
    position = PassivePosition(
        position_id="POS-GROUP-TASK",
        emitter_track_id="EMITTER-GROUP-TASK",
        burst_id="BURST-1",
        sample_id="SAMPLE-1",
        observed_at_min=1.0,
        position_cells=(5.0, 5.0),
        source_observation_ids=("OBS-GROUP-TASK", "OBS-GROUP-TASK-2"),
    )
    same_group = PassiveBearingObservation(
        observation_id="OBS-GROUP-TASK",
        sample_id="SAMPLE-1",
        emitter_track_id="EMITTER-GROUP-TASK",
        burst_id="BURST-1",
        observed_at_min=1.0,
        observer_uav_id="UAV-1",
        observer_position_cells=(3.0, 5.0),
        bearing_deg=0.0,
        bearing_std_deg=3.0,
    )
    different_group = PassiveBearingObservation(
        observation_id="OBS-DIFFERENT-GROUP",
        sample_id="SAMPLE-2",
        emitter_track_id="EMITTER-GROUP-TASK",
        burst_id="BURST-2",
        observed_at_min=1.0,
        observer_uav_id="UAV-2",
        observer_position_cells=(3.0, 5.0),
        bearing_deg=0.0,
        bearing_std_deg=3.0,
    )
    state.current_time = 1.0
    state.record_passive_observations((same_group, different_group))
    state.apply_information_facts([position, same_group, different_group], 1.0)
    state.register_passive_position(position)

    catalog = TaskCatalog(candidate_extractor=_EmptyExtractor())
    tasks = catalog.build(state, (), (), now_min=1.0)

    assert not any(task.task_id == "direction:OBS-GROUP-TASK" for task in tasks)
    assert any(task.task_id == "direction:OBS-DIFFERENT-GROUP" for task in tasks)
