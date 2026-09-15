from __future__ import annotations

from src.control.common.contracts import (
    ActionSpec,
    ControlEvent,
    ControlOwner,
    ControlTask,
    ObservationSpec,
    OperationMode,
)
from src.control.common.factory import ControlFactory
from src.control.common.ownership import ControlOwnership
from src.control.heuristic.coverage import CoverageController
from src.control.heuristic.task_flow import HeuristicTaskFlow
from src.mission.contracts import Assessment, VisualDetection
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import GridCoord
from src.schedule.state_manager import StateManager


def test_civilian_release_clears_contact_region_and_uav_without_resuming_search():
    config = ConfigLoader.load()
    state = StateManager(config)
    contact_id = state.contacts.ingest_visual(
        VisualDetection(
            "EO-1", 0.0, "eo", "UAV-1", (10.0, 10.0), None, 0.1,
            (8.0, 10.0), 2.0, "open_water",
        )
    )
    state.contacts.reserve(contact_id, "UAV-1", "P0001")
    contact = state.contacts.snapshot(contact_id)
    state.contacts.apply_assessment(Assessment(
        "A0001", contact_id, "P0001", contact.revision, 1.0, "civilian", 0.9,
        ("EO-1",), ("near observation supports civilian behavior",), (), "call-1",
    ))
    region = state.create_track_region(contact_id, GridCoord(10, 10))
    uav = state.get_uav("UAV-1")
    assert uav is not None
    uav.assigned_region_id = region.id
    uav.target_group_id = contact_id

    factory = ControlFactory(
        config.control,
        observation_spec=ObservationSpec("control-observation/v2", 11),
        action_spec=ActionSpec(-2.0, 2.0, 0.5, 1.0),
    )
    ownership = ControlOwnership(("UAV-1",))
    lease = ownership.acquire("UAV-1", ControlOwner.HEURISTIC, "probe:P0001", 0.0)
    coverage = ControlTask("search:S1", OperationMode.COVERAGE,
                           region_bbox=region.bbox)
    controllers = {"UAV-1": CoverageController(
        observation_spec=ObservationSpec("control-observation/v2", 11),
        action_spec=ActionSpec(-2.0, 2.0, 0.5, 1.0),
    )}
    pending = {"UAV-1": ControlTask(
        "probe:C0001", OperationMode.PROBE, target_contact_id=contact_id, probe_id="P0001"
    )}
    flow = HeuristicTaskFlow(ownership, factory, controllers, pending, state_manager=state)
    flow._saved_coverage_tasks["UAV-1"] = coverage

    transition = flow.handle(ControlEvent(
        1, 1.0, "mission_task_released", "assessment", "UAV-1",
        {"contact_id": contact_id, "probe_id": "P0001", "identity": "civilian"},
    ), lease)

    assert state.contacts.snapshot(contact_id).assigned_uav_id is None
    assert state.get_track_region_for_group(contact_id) is None
    assert state.get_uav("UAV-1").assigned_region_id is None
    assert state.get_uav("UAV-1").target_group_id is None
    assert transition.current_lease.owner is ControlOwner.SYSTEM
    assert transition.task is not None and transition.task.task_type is OperationMode.HOLDING
    assert "UAV-1" not in flow._saved_coverage_tasks
    assert transition.request_assignment
    assert state.get_recent_events(0.0)[-1]["type"] == "resource_available"
