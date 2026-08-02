import math

from src.utils.conflict_detector import PathConflict, detect_conflicts, resolve_conflicts


def test_conflict_detector_identifies_same_time_same_cell():
    conflicts = detect_conflicts([
        {"id": "UAV-1", "status": "transit", "planned_path": [(0.0, 0.0, 0.0)] * 5 + [(1.0, 1.0, 0.0)]},
        {"id": "UAV-2", "status": "searching", "planned_path": [(0.0, 0.0, 0.0)] * 5 + [(1.1, 1.0, 0.0)]},
    ], min_separation_cells=0.5)

    assert len(conflicts) == 1
    assert {conflicts[0].uav_a, conflicts[0].uav_b} == {"UAV-1", "UAV-2"}


def test_conflict_detector_ignores_current_pose_overlap():
    conflicts = detect_conflicts([
        {"id": "UAV-1", "status": "transit", "planned_path": [(1.0, 1.0, 0.0)]},
        {"id": "UAV-2", "status": "transit", "planned_path": [(1.0, 1.0, 0.0)]},
    ])

    assert conflicts == []


def test_conflict_detector_catches_a_mid_step_crossing():
    conflicts = detect_conflicts([
        {"id": "UAV-1", "status": "transit", "planned_path": [
            (0.0, 0.0, 0.0), (2.0, 0.0, 0.0),
        ]},
        {"id": "UAV-2", "status": "transit", "planned_path": [
            (2.0, 0.0, math.pi), (0.0, 0.0, math.pi),
        ]},
    ], min_separation_cells=0.5)

    assert len(conflicts) == 1
    assert conflicts[0].cell == (1, 0)
    assert conflicts[0].distance_cells == 0.0


def test_conflict_resolver_preserves_tracking_airframe_priority():
    class Entity:
        def __init__(self, status, fuel):
            self.status = status
            self.fuel_remaining_pct = fuel

    conflicts = [PathConflict("UAV-1", "UAV-2", (3, 4), 0, 0, 0.1)]
    replan = resolve_conflicts(conflicts, {
        "UAV-1": Entity("tracking", 0.1),
        "UAV-2": Entity("searching", 1.0),
    })

    assert replan == ["UAV-2"]
